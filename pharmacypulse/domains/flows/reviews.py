"""Review submission, pharmacist responses, moderation, flagging."""
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
from django.db import IntegrityError, transaction
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

from .common import admin_required, _activity, safe_next


def submit_review(request):
    # The review popup submits via AJAX (returns JSON); plain form posts keep
    # the legacy redirect-to-confirmation behavior.
    is_ajax = (request.headers.get("X-Requested-With") == "XMLHttpRequest"
               or "application/json" in (request.headers.get("Accept") or ""))

    def _redirect_back(error: str = "") -> HttpResponse:
        url = (f"/review?pharmacy_id={pharmacy_id}"
               f"&pharmacy_name={urllib.parse.quote(pharmacy_name)}")
        if error:
            url += f"&error={urllib.parse.quote(error)}"
        return redirect(url)

    def _json(ok: bool, *, error: str = "", review_id: Any = None,
              message: str = "", status: int = 200) -> JsonResponse:
        payload = {"ok": ok}
        if ok:
            payload["review_id"] = review_id
            payload["message"] = message or "Thank you — your review is live."
        else:
            payload["error"] = error or "Could not submit review."
        return JsonResponse(payload, status=status)

    user = request.user if request.user.is_authenticated else None
    # Per-IP review submission cap: 10/hour.
    from ...views_extra import is_rate_limited
    if is_rate_limited(request, "review", limit=10, window_s=3600):
        err = "Too many reviews submitted recently. Try again in an hour."
        if is_ajax:
            return _json(False, error=err, status=429)
        return redirect(f"/review?error={urllib.parse.quote(err)}")
    try:
        pharmacy_id = int(request.POST.get("pharmacy_id") or 0)
    except (TypeError, ValueError):
        if is_ajax:
            return _json(False, error="pharmacy_id required", status=400)
        return HttpResponseBadRequest("pharmacy_id required")
    if not pharmacy_id:
        if is_ajax:
            return _json(False, error="pharmacy_id required", status=400)
        return HttpResponseBadRequest("pharmacy_id required")
    pharmacy_name = request.POST.get("pharmacy_name") or ""
    # One review per user per pharmacy (authenticated users only)
    if user:
        existing = Review.objects.filter(
            pharmacy_id=pharmacy_id, user=user, deleted_at__isnull=True,
        ).exists()
        if existing:
            if is_ajax:
                return _json(False,
                             error="You have already reviewed this pharmacy.",
                             status=409)
            return _redirect_back("You have already reviewed this pharmacy.")
    # For anonymous users, derive user_name from POST; for authenticated, use profile
    if user:
        user_name = f"{user.first_name} {user.last_name}".strip() or user.email
    else:
        user_name = (request.POST.get("user_name") or "").strip()[:100]
        if not user_name:
            user_name = "Anonymous Patient"
    from django.core.exceptions import ValidationError
    try:
        review = Review.objects.create(
            pharmacy_id=pharmacy_id, pharmacy_name=pharmacy_name,
            user=user, user_name=user_name,
            stock_available=int(request.POST.get("stock_available") or 1),
            wait_time_rating=int(request.POST.get("wait_time_rating") or 3),
            service_rating=int(request.POST.get("service_rating") or 3),
            comment=request.POST.get("comment") or "",
            is_caregiver=int(request.POST.get("is_caregiver") or 0),
        )
    except ValidationError as exc:
        msg = (exc.messages[0] if getattr(exc, "messages", None)
               else "Could not submit review")
        if is_ajax:
            return _json(False, error=msg, status=400)
        return _redirect_back(msg)
    except IntegrityError:
        # Race-condition backstop for the (pharmacy, user) partial unique
        # constraint — a second review slipped in between the pre-check above.
        if is_ajax:
            return _json(False, error="You have already reviewed this pharmacy.", status=409)
        return _redirect_back("You have already reviewed this pharmacy.")
    except Exception as exc:
        if is_ajax:
            return _json(False, error=str(exc), status=500)
        return _redirect_back(str(exc))
    if is_ajax:
        return _json(True, review_id=review.id)
    return redirect(f"/review?pharmacy_id={pharmacy_id}&pharmacy_name={urllib.parse.quote(pharmacy_name)}&success=1")


@login_required
def respond_to_review(request, review_id: int):
    review = Review.objects.filter(id=review_id).first()
    if not review or not review.pharmacy_id:
        return redirect("/pharmacist-dashboard?tab=reviews&error=Review+not+found")

    month = djtz.now().strftime("%Y-%m")
    used = (ResponseCount.objects.filter(pharmacy_id=review.pharmacy_id, month=month)
            .aggregate(t=Sum("count"))["t"] or 0)
    if used >= 10:
        return redirect(
            "/pharmacist-dashboard?tab=upgrade&error=Free+response+limit+reached+(10+responses+this+month).+Upgrade+to+continue."
        )
    response_text = (request.POST.get("response_text") or "").strip()
    if not response_text:
        return redirect("/pharmacist-dashboard?tab=reviews&error=Response+required")

    # PHI / legal screening on pharmacist response
    from ...models import ModerationKeyword
    bad = ModerationKeyword.objects.filter(category__in=("medication", "legal"))
    low = response_text.lower()
    if any(kw.keyword.lower() in low for kw in bad):
        return redirect(
            "/pharmacist-dashboard?tab=reviews&error=Your+response+may+contain+medication+names+or+protected+health+information.+Please+revise."
        )

    first_response = not (review.response_text or "").strip()
    Review.objects.filter(id=review_id).update(
        response_text=response_text, response_date=djtz.now().isoformat(),
    )
    if first_response:
        counter, created = ResponseCount.objects.get_or_create(
            pharmacy_id=review.pharmacy_id,
            month=month,
            defaults={"count": 1},
        )
        if not created:
            ResponseCount.objects.filter(pk=counter.pk).update(count=F("count") + 1)
    # Look up responder org
    tm = (PharmacyTeamMember.objects.filter(user=request.user, accepted_at__isnull=False)
          .first())
    ReviewResponse.objects.create(
        review_id=review_id, responder=request.user,
        responder_name=f"{request.user.first_name} {request.user.last_name}".strip(),
        org=(tm.org if tm else None),
    )
    _activity(request, "review_response", "Pharmacist responded to a review")
    if review.user_id:
        Notification.objects.create(
            user_id=review.user_id, title="Pharmacist Response",
            body="The pharmacy responded to your review.",
        )
    return redirect("/pharmacist-dashboard?tab=reviews&msg=Response+sent")


_MODERATION_ACTIONS = {"approved", "flagged", "rejected", "pending"}


@admin_required
def moderate_review(request, review_id: int):
    action = request.POST.get("action", "approved")
    if action not in _MODERATION_ACTIONS:
        action = "flagged"
    Review.objects.filter(id=review_id).update(moderation_status=action)
    _activity(request, "review_moderated",
              f"Review #{review_id} {action} by {request.user.email}")
    return JsonResponse({"status": action})


_FLAG_REASONS = {"wrong_pharmacy", "fraudulent", "spam", "inaccurate", "other"}


@login_required
def flag_review(request, review_id: int):
    reason = (request.POST.get("reason") or "other").strip()
    if reason not in _FLAG_REASONS:
        reason = "other"
    note = (request.POST.get("note") or "").strip()[:500]
    review = Review.objects.filter(id=review_id, deleted_at__isnull=True).first()
    if not review:
        return redirect(safe_next(request, "/"))
    # Only re-flag if not already flagged or rejected — keeps admin queue clean.
    if review.moderation_status not in ("flagged", "rejected"):
        Review.objects.filter(id=review_id).update(
            moderation_status="flagged",
            flag_reason=f"{reason}|{request.user.email}|{note}" if note else f"{reason}|{request.user.email}",
            flagged_at=djtz.now().isoformat(),
        )
        _activity(request, "review_flagged",
                  f"Review #{review_id} flagged as {reason} by {request.user.email}")
    next_url = safe_next(request, "/")
    return redirect(f"{next_url}{'&' if '?' in next_url else '?'}flag=ok")
