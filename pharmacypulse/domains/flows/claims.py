"""Pharmacy claim verification + admin approve/reject."""
from __future__ import annotations

import json
import re
import secrets
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from django.conf import settings
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import F, Sum
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse, HttpResponseBadRequest
from django.shortcuts import redirect
from django.utils import timezone as djtz
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from ...models import (
    ActivityLog, DataRequest, DrugShortage, NewsletterSubscriber, Notification,
    Pharmacy, PharmacyClaim, PharmacyHours, PharmacyOrg, PharmacyTeamMember,
    ResponseCount, Review, ReviewResponse, SavedComparison, User, UserConsent,
)

from .common import admin_required, _activity


@login_required
def verify_and_submit_claim(request, pharmacy_id: int):
    npi_number = (request.POST.get("npi_number") or "").strip()
    license_number = (request.POST.get("license_number") or "").strip()
    pharmacy_name = (request.POST.get("pharmacy_name") or "").strip()
    user = request.user
    # Throttle: 5 claim attempts per IP per hour. Each calls the NPI registry
    # so this is also rate-limit defense against API abuse.
    from ...views_extra import is_rate_limited
    if is_rate_limited(request, "claim", limit=5, window_s=3600):
        back = f"/claim?pharmacy_id={pharmacy_id}&error=Too+many+claim+attempts.+Try+again+in+an+hour."
        return redirect(back)

    # Block duplicate pending claims — a user may only have one active pending
    # claim at a time. Approved/rejected claims do not block a new submission.
    if PharmacyClaim.objects.filter(user=user, status="pending").exists():
        back = (
            f"/claim?pharmacy_id={pharmacy_id}"
            f"&pharmacy_name={urllib.parse.quote(pharmacy_name)}"
            f"&error=You+already+have+a+pending+claim+under+review."
            f"+Please+wait+for+our+team+to+process+it+before+submitting+another."
        )
        return redirect(back)

    if not (npi_number and license_number and pharmacy_name):
        back = f"/claim?pharmacy_id={pharmacy_id}&pharmacy_name={urllib.parse.quote(pharmacy_name)}&error=Missing+required+fields"
        return redirect(back)

    # SECURITY: never auto-grant the `pharmacist` role or claim a pharmacy
    # based solely on NPI existence in the public registry — anyone can look
    # up a valid NPI number and would otherwise be able to claim a pharmacy
    # they don't operate. Confirmation is an admin decision (approve_claim /
    # reject_claim) after checking the submitted license + ownership. Claim
    # is always created as `pending` regardless of NPI lookup result.
    claim = PharmacyClaim.objects.create(
        pharmacy_id=pharmacy_id, pharmacy_name=pharmacy_name,
        user=user, user_name=f"{user.first_name} {user.last_name}".strip(),
        npi_number=npi_number, license_number=license_number,
        status="pending",
    )
    _activity(request, "claim_submitted",
              f"Claim for {pharmacy_name} (NPI {npi_number}) pending admin review")
    return redirect(
        f"/claim?pharmacy_id={pharmacy_id}&pending=1"
        f"&pharmacy_name={urllib.parse.quote(pharmacy_name)}"
        "&msg=Your+claim+has+been+submitted+for+review."
    )


@admin_required
def approve_claim(request, claim_id: int):
    claim = PharmacyClaim.objects.filter(id=claim_id).first()
    if not claim:
        return JsonResponse({"error": "not found"}, status=404)
    claim.status = "approved"
    claim.reviewed_at = djtz.now().isoformat()
    claim.reviewed_by = request.user.email
    claim.save()
    org = PharmacyOrg.objects.create(name=claim.pharmacy_name or "", owner_id=claim.user_id)
    Pharmacy.objects.filter(id=claim.pharmacy_id).update(claimed_by=claim.user_id, org_id=org.id)
    claim_user = User.objects.filter(id=claim.user_id).first()
    PharmacyTeamMember.objects.create(
        org=org, user_id=claim.user_id,
        email=(claim_user.phone if claim_user else "") or (claim_user.email if claim_user else ""),
        role="owner", accepted_at=djtz.now(),
    )
    User.objects.filter(id=claim.user_id).update(role="pharmacist")
    _activity(request, "claim_approved",
              f"Claim for {claim.pharmacy_name} approved by {request.user.email}")
    if claim.user_id:
        Notification.objects.create(
            user_id=claim.user_id, title="Claim Approved",
            body=(f"Your claim for {claim.pharmacy_name} has been approved. "
                  "Visit your Pharmacy Owner Dashboard to manage your listing."),
        )
        # Send transactional email — failure must never break the approve flow
        if claim_user and claim_user.email:
            base = settings.BRAND.get("url", "").rstrip("/")
            try:
                send_mail(
                    subject="Your pharmacy claim has been approved — PharmacyPulse",
                    message=(
                        f"Hi {claim_user.first_name or 'there'},\n\n"
                        f"Great news! Your claim for {claim.pharmacy_name} has been approved.\n\n"
                        f"You now have full access to your Pharmacy Owner Dashboard where you can:\n"
                        f"  • Respond to patient reviews\n"
                        f"  • View analytics and ratings\n"
                        f"  • Manage your listing details\n\n"
                        f"Visit your dashboard here:\n{base}/pharmacist-dashboard\n\n"
                        f"Welcome to PharmacyPulse.\n\n"
                        f"— The PharmacyPulse Team"
                    ),
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[claim_user.email],
                    fail_silently=True,
                )
            except Exception:
                pass  # never block the approve flow on email errors
    return JsonResponse({"status": "approved", "claim_id": claim_id})


@admin_required
def reject_claim(request, claim_id: int):
    claim = PharmacyClaim.objects.filter(id=claim_id).first()
    if not claim:
        return JsonResponse({"error": "not found"}, status=404)
    reason = request.POST.get("reason", "")
    claim.status = "rejected"
    claim.reviewed_at = djtz.now().isoformat()
    claim.reviewed_by = request.user.email
    claim.rejection_reason = reason
    claim.save()
    _activity(request, "claim_rejected", f"Claim for {claim.pharmacy_name} rejected")
    if claim.user_id:
        Notification.objects.create(
            user_id=claim.user_id, title="Claim Rejected",
            body=f"Your claim for {claim.pharmacy_name} was not approved. Reason: {reason}",
        )
        # Send transactional email — fetch the user here since reject_claim
        # doesn't look them up above (approve_claim does for the team member step)
        claim_user = User.objects.filter(id=claim.user_id).first()
        if claim_user and claim_user.email:
            base = settings.BRAND.get("url", "").rstrip("/")
            try:
                send_mail(
                    subject="Update on your PharmacyPulse pharmacy claim",
                    message=(
                        f"Hi {claim_user.first_name or 'there'},\n\n"
                        f"We've reviewed your claim for {claim.pharmacy_name} "
                        f"and were unable to approve it at this time.\n\n"
                        + (f"Reason: {reason}\n\n" if reason else "")
                        + f"If you believe this is an error or would like to submit updated "
                        f"documentation, you can reapply here:\n{base}/claim\n\n"
                        f"If you have any questions, reply to this email and our team will help.\n\n"
                        f"— The PharmacyPulse Team"
                    ),
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[claim_user.email],
                    fail_silently=True,
                )
            except Exception:
                pass  # never block the reject flow on email errors
    return JsonResponse({"status": "rejected", "claim_id": claim_id})
