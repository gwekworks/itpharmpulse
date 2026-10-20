"""Stripe checkout, billing portal, webhook."""
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
def create_checkout(request):
    if not settings.STRIPE_SECRET_KEY or not settings.STRIPE_PRICE_ID:
        return redirect("/pharmacist-dashboard?tab=upgrade&error=Stripe+is+not+configured")
    base = request.build_absolute_uri("/").rstrip("/")
    payload = {
        "mode": "subscription",
        "line_items[0][price]": settings.STRIPE_PRICE_ID,
        "line_items[0][quantity]": 1,
        "success_url": f"{base}/pharmacist-dashboard?tab=upgrade&upgraded=1",
        "cancel_url": f"{base}/pharmacist-dashboard?tab=upgrade",
        "client_reference_id": str(request.user.id),
        "metadata[user_id]": str(request.user.id),  # belt-and-suspenders for webhook lookup
        "subscription_data[metadata][user_id]": str(request.user.id),
        "allow_promotion_codes": "true",
    }
    # Reuse the Stripe customer if we already have one, so subscription history
    # stays attached to the same customer in the Stripe dashboard.
    if request.user.stripe_customer_id:
        payload["customer"] = request.user.stripe_customer_id
    else:
        payload["customer_email"] = request.user.email
    try:
        r = requests.post(
            "https://api.stripe.com/v1/checkout/sessions",
            headers={"Authorization": f"Bearer {settings.STRIPE_SECRET_KEY}",
                     "Content-Type": "application/x-www-form-urlencoded"},
            data=payload, timeout=15,
        )
        url = r.json().get("url")
    except requests.RequestException:
        url = None
    if not url:
        return redirect("/pharmacist-dashboard?tab=upgrade&error=Stripe+checkout+failed")
    return redirect(url)


@login_required
def billing_portal(request):
    """Redirect to Stripe's hosted Customer Portal — lets users update card,
    cancel subscription, view invoices. No state changes happen here; cancellation
    flows back through the webhook just like checkout."""
    if not settings.STRIPE_SECRET_KEY:
        return redirect("/pharmacist-dashboard?tab=upgrade&error=Stripe+is+not+configured")
    if not request.user.stripe_customer_id:
        return redirect("/pharmacist-dashboard?tab=upgrade&error=No+active+subscription")
    base = request.build_absolute_uri("/").rstrip("/")
    try:
        r = requests.post(
            "https://api.stripe.com/v1/billing_portal/sessions",
            headers={"Authorization": f"Bearer {settings.STRIPE_SECRET_KEY}",
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={
                "customer": request.user.stripe_customer_id,
                "return_url": f"{base}/pharmacist-dashboard?tab=upgrade",
            }, timeout=15,
        )
        url = r.json().get("url")
    except requests.RequestException:
        url = None
    if not url:
        return redirect("/pharmacist-dashboard?tab=upgrade&error=Could+not+open+billing+portal")
    return redirect(url)


@csrf_exempt
def stripe_webhook(request):
    """Handle Stripe subscription lifecycle events.

    csrf_exempt is required: Stripe posts without a CSRF token. Security is
    provided by the Stripe-Signature HMAC verification below — never remove
    the signature check on this endpoint.

    Events handled:
      - checkout.session.completed     → first-time upgrade, set plan=pro
      - customer.subscription.updated  → status / period_end changes
      - customer.subscription.deleted  → downgrade to free
      - invoice.payment_failed         → mark past_due (still pro until period end)
    """
    import hmac, hashlib, time as _time
    from django.http import HttpResponse

    secret = settings.STRIPE_WEBHOOK_SECRET
    if not secret:
        return HttpResponseBadRequest("Webhook secret not configured")

    sig_header = request.META.get("HTTP_STRIPE_SIGNATURE", "")
    payload = request.body  # raw bytes — must NOT be re-encoded

    # Parse the t= and v1= fields out of Stripe's signature header.
    parts = dict(p.split("=", 1) for p in sig_header.split(",") if "=" in p)
    timestamp = parts.get("t", "")
    sent_sig = parts.get("v1", "")
    if not timestamp or not sent_sig:
        return HttpResponseBadRequest("Bad signature header")

    # Reject events older than 5 minutes (replay protection).
    try:
        if abs(_time.time() - int(timestamp)) > 300:
            return HttpResponseBadRequest("Timestamp too old")
    except ValueError:
        return HttpResponseBadRequest("Bad timestamp")

    signed = f"{timestamp}.".encode() + payload
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sent_sig):
        return HttpResponseBadRequest("Bad signature")

    try:
        event = json.loads(payload)
    except json.JSONDecodeError:
        return HttpResponseBadRequest("Bad JSON")

    etype = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}

    def _user_for(obj):
        # Try metadata.user_id first (most reliable — set during checkout),
        # fall back to customer-id lookup, fall back to email.
        meta = (obj.get("metadata") or {}).get("user_id")
        if meta and meta.isdigit():
            u = User.objects.filter(id=int(meta)).first()
            if u:
                return u
        cid = obj.get("customer")
        if cid:
            u = User.objects.filter(stripe_customer_id=cid).first()
            if u:
                return u
        email = obj.get("customer_email") or (obj.get("customer_details") or {}).get("email")
        if email:
            return User.objects.filter(email=email.lower()).first()
        return None

    if etype == "checkout.session.completed":
        u = _user_for(obj)
        if u:
            u.stripe_customer_id = obj.get("customer", "") or u.stripe_customer_id
            u.stripe_subscription_id = obj.get("subscription", "") or u.stripe_subscription_id
            u.plan = "pro"
            u.subscription_status = "active"
            u.save(update_fields=[
                "stripe_customer_id", "stripe_subscription_id",
                "plan", "subscription_status",
            ])

    elif etype in ("customer.subscription.updated", "customer.subscription.created"):
        u = _user_for(obj)
        if u:
            status = obj.get("status", "")
            period_end = obj.get("current_period_end")
            u.stripe_subscription_id = obj.get("id", "") or u.stripe_subscription_id
            u.subscription_status = status
            if period_end:
                u.subscription_period_end = datetime.fromtimestamp(period_end, tz=timezone.utc)
            # Active OR trialing OR past_due → still grant pro until period_end.
            # canceled/unpaid/incomplete_expired → downgrade.
            if status in ("active", "trialing", "past_due"):
                u.plan = "pro"
            else:
                u.plan = "free"
            u.save(update_fields=[
                "stripe_subscription_id", "subscription_status",
                "subscription_period_end", "plan",
            ])

    elif etype == "customer.subscription.deleted":
        u = _user_for(obj)
        if u:
            u.plan = "free"
            u.subscription_status = "canceled"
            u.save(update_fields=["plan", "subscription_status"])

    elif etype == "invoice.payment_failed":
        u = _user_for(obj)
        if u:
            u.subscription_status = "past_due"
            u.save(update_fields=["subscription_status"])

    return HttpResponse(status=200)
