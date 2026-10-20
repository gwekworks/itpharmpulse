"""Newsletter subscribe / unsubscribe (signed URLs)."""
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
def newsletter_subscribe(request):
    from ... import resend_audience
    u = request.user
    zip_code = request.POST.get("zip_code", u.zip_code or "")
    sub, _ = NewsletterSubscriber.objects.get_or_create(
        email=u.email, defaults={"user": u, "zip_code": zip_code},
    )
    if not sub.subscribed:
        sub.subscribed = 1; sub.unsubscribed_at = None
        if not sub.user_id:
            sub.user = u
        sub.save()
    resend_audience.add_contact(
        email=u.email, first_name=u.first_name or "", last_name=u.last_name or "",
    )
    _activity(request, "newsletter_subscribe", "Subscribed to newsletter")
    return JsonResponse({"ok": True})


@login_required
def newsletter_unsubscribe(request):
    from ... import resend_audience
    NewsletterSubscriber.objects.filter(user=request.user).update(
        subscribed=0, unsubscribed_at=djtz.now(),
    )
    resend_audience.unsubscribe(request.user.email)
    _activity(request, "newsletter_unsubscribe", "Unsubscribed from newsletter")
    return JsonResponse({"ok": True})


def newsletter_unsubscribe_public(request):
    from django.core.signing import BadSignature, SignatureExpired, TimestampSigner
    from ... import resend_audience
    token = (request.GET.get("t") or "").strip()
    email = None
    if token:
        try:
            email = TimestampSigner(salt="pp-newsletter").unsign(token, max_age=60 * 60 * 24 * 365)
        except (BadSignature, SignatureExpired):
            email = None
    if email:
        NewsletterSubscriber.objects.filter(email=email).update(
            subscribed=0, unsubscribed_at=djtz.now(),
        )
        resend_audience.unsubscribe(email)
        ActivityLog.objects.create(
            type="newsletter_unsubscribe",
            message=f"Public unsubscribe: {email}",
        )
    return HttpResponse(
        "<html><head><title>Unsubscribed — PharmacyPulse</title></head>"
        "<body style='font-family:system-ui;max-width:32rem;margin:4rem auto;padding:0 1rem'>"
        "<h1>You're unsubscribed.</h1>"
        "<p>You won't receive any more PharmacyPulse emails. "
        "<a href='/' style='color:#008F82'>Back to PharmacyPulse</a>.</p>"
        "</body></html>",
        content_type="text/html",
    )


def make_unsubscribe_url(email: str, base: str) -> str:
    """Helper for emails: returns a signed one-click unsub URL for this email."""
    from django.core.signing import TimestampSigner
    token = TimestampSigner(salt="pp-newsletter").sign(email)
    return f"{base.rstrip('/')}/newsletter/unsubscribe?t={token}"
