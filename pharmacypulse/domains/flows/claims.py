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

    if not (npi_number and license_number and pharmacy_name):
        back = f"/claim?pharmacy_id={pharmacy_id}&pharmacy_name={urllib.parse.quote(pharmacy_name)}&error=Missing+required+fields"
        return redirect(back)

    # ──────────────────────────────────────────────────────────────────
    # REAL-TIME NPI VERIFICATION via NPPES Registry
    # ──────────────────────────────────────────────────────────────────
    npi_verified = False
    npi_verification_details = ""
    auto_approved = False
    
    try:
        # Query NPPES API to verify NPI
        nppes_response = requests.get(
            "https://npiregistry.cms.hhs.gov/api/",
            params={"version": "2.1", "number": npi_number},
            timeout=10
        )
        
        if nppes_response.ok:
            nppes_data = nppes_response.json()
            results = nppes_data.get("results", [])
            
            if results and len(results) > 0:
                provider = results[0]
                basic = provider.get("basic", {})
                
                # Extract pharmacy name from NPPES
                nppes_org_name = (basic.get("organization_name") or "").strip()
                nppes_name = nppes_org_name
                
                # Check for DBA (Doing Business As) name
                for other in provider.get("other_names", []) or []:
                    if (other.get("type") or "").lower().startswith("doing business as"):
                        dba_name = (other.get("organization_name") or "").strip()
                        if dba_name:
                            nppes_name = dba_name
                            break
                
                # Check if provider is a pharmacy (has pharmacy taxonomy)
                is_pharmacy = False
                pharmacy_taxonomies = {
                    "3336C0003X", "3336M0002X", "3336S0011X", "3336C0004X",
                    "3336L0003X", "3336H0001X", "3336C0002X", "333600000X"
                }
                for taxonomy in provider.get("taxonomies", []) or []:
                    if (taxonomy.get("code") or "").strip() in pharmacy_taxonomies:
                        is_pharmacy = True
                        break
                
                # Verify NPI status is active
                npi_status = basic.get("status", "")
                is_active = npi_status == "A"
                
                # Get pharmacy from database to match
                pharmacy = Pharmacy.objects.filter(id=pharmacy_id).first()
                
                # Verification logic:
                # 1. NPI must be active
                # 2. Provider must have pharmacy taxonomy
                # 3. Pharmacy name should match (fuzzy match for flexibility)
                if is_active and is_pharmacy and pharmacy:
                    # Fuzzy name match (case-insensitive, ignore punctuation)
                    def normalize_name(name):
                        return re.sub(r'[^a-z0-9]', '', name.lower())
                    
                    nppes_normalized = normalize_name(nppes_name)
                    claim_normalized = normalize_name(pharmacy_name)
                    db_normalized = normalize_name(pharmacy.name)
                    
                    # Check if names match (claimed name matches NPPES OR DB)
                    name_matches = (
                        nppes_normalized in claim_normalized or 
                        claim_normalized in nppes_normalized or
                        db_normalized in claim_normalized or
                        claim_normalized in db_normalized
                    )
                    
                    if name_matches:
                        npi_verified = True
                        npi_verification_details = f"NPPES verified: {nppes_name} (Active)"
                    else:
                        npi_verification_details = f"Name mismatch: NPPES shows '{nppes_name}'"
                else:
                    if not is_active:
                        npi_verification_details = "NPI is not active in NPPES registry"
                    elif not is_pharmacy:
                        npi_verification_details = "NPI is not registered as a pharmacy"
                    else:
                        npi_verification_details = "Pharmacy not found in database"
            else:
                npi_verification_details = "NPI not found in NPPES registry"
        else:
            npi_verification_details = f"NPPES API error: {nppes_response.status_code}"
    
    except requests.RequestException as e:
        npi_verification_details = f"NPPES API connection error: {str(e)}"
    except Exception as e:
        npi_verification_details = f"Verification error: {str(e)}"

    # ──────────────────────────────────────────────────────────────────
    # CREATE CLAIM with verification status
    # ──────────────────────────────────────────────────────────────────
    if npi_verified:
        # AUTO-APPROVE: NPI verification passed
        with transaction.atomic():
            claim = PharmacyClaim.objects.create(
                pharmacy_id=pharmacy_id, pharmacy_name=pharmacy_name,
                user=user, user_name=f"{user.first_name} {user.last_name}".strip(),
                npi_number=npi_number, license_number=license_number,
                status="approved",
                reviewed_at=djtz.now().isoformat(),
                reviewed_by="AUTO-VERIFIED via NPPES",
            )
            
            # Create pharmacy org and assign ownership
            org = PharmacyOrg.objects.create(name=pharmacy_name or "", owner_id=user.id)
            Pharmacy.objects.filter(id=pharmacy_id).update(claimed_by=user.id, org_id=org.id)
            
            # Add user to team
            PharmacyTeamMember.objects.create(
                org=org, user_id=user.id,
                email=user.email or user.phone or "",
                role="owner", accepted_at=djtz.now(),
            )
            
            # Grant pharmacist role
            User.objects.filter(id=user.id).update(role="pharmacist")
            
            # Notify user
            Notification.objects.create(
                user_id=user.id, title="Claim Approved (Auto-Verified)",
                body=(f"Your claim for {pharmacy_name} has been automatically approved "
                      "via NPI verification. Welcome to your Pharmacy Owner Dashboard!"),
            )
            
            # Send approval email
            if user.email:
                base = settings.BRAND.get("url", "").rstrip("/")
                try:
                    send_mail(
                        subject="Your pharmacy claim has been approved — PharmacyPulse",
                        message=(
                            f"Hi {user.first_name or 'there'},\n\n"
                            f"Great news! Your claim for {pharmacy_name} has been automatically "
                            f"approved via NPI verification.\n\n"
                            f"You now have full access to your Pharmacy Owner Dashboard where you can:\n"
                            f"  • Respond to patient reviews\n"
                            f"  • View analytics and ratings\n"
                            f"  • Manage your listing details\n\n"
                            f"Visit your dashboard here:\n{base}/pharmacist-dashboard\n\n"
                            f"Welcome to PharmacyPulse.\n\n"
                            f"— The PharmacyPulse Team"
                        ),
                        from_email=settings.DEFAULT_FROM_EMAIL,
                        recipient_list=[user.email],
                        fail_silently=True,
                    )
                except Exception:
                    pass
            
            # Log activity with auto-approval flag
            _activity(request, "claim_auto_approved",
                      f"Claim for {pharmacy_name} (NPI {npi_number}) AUTO-APPROVED via NPPES. {npi_verification_details}")
            
            # Notify admin about auto-approval
            admin_users = User.objects.filter(role="admin")
            for admin in admin_users:
                Notification.objects.create(
                    user_id=admin.id, 
                    title="Auto-Approved Claim (NPI Verified)",
                    body=(f"✅ {user.first_name} {user.last_name} claimed {pharmacy_name} "
                          f"(NPI: {npi_number}). Auto-approved via NPPES verification."),
                )
            
            auto_approved = True
        
        return redirect(
            f"/claim?pharmacy_id={pharmacy_id}&approved=1"
            f"&pharmacy_name={urllib.parse.quote(pharmacy_name)}"
            "&msg=Your+claim+has+been+approved!+Welcome+to+your+dashboard."
        )
    
    else:
        # MANUAL REVIEW: NPI verification failed or didn't match
        claim = PharmacyClaim.objects.create(
            pharmacy_id=pharmacy_id, pharmacy_name=pharmacy_name,
            user=user, user_name=f"{user.first_name} {user.last_name}".strip(),
            npi_number=npi_number, license_number=license_number,
            status="pending",
        )
        
        # Log activity with verification failure details
        _activity(request, "claim_submitted",
                  f"Claim for {pharmacy_name} (NPI {npi_number}) pending MANUAL review. "
                  f"NPI verification: {npi_verification_details}")
        
        # Notify admin about pending claim requiring manual review
        admin_users = User.objects.filter(role="admin")
        for admin in admin_users:
            Notification.objects.create(
                user_id=admin.id, 
                title="Manual Review Required: Pharmacy Claim",
                body=(f"⚠️ {user.first_name} {user.last_name} claimed {pharmacy_name} "
                      f"(NPI: {npi_number}). NPI verification inconclusive - manual review needed."),
            )
        
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
