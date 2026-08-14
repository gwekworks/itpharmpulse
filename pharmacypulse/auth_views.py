"""
Auth routes: email/password login + signup + Google OAuth.

Benmore app.yaml:
  identifier: email
  signup_fields: "full_name,zip_code"  # full_name split into first/last on save
  session_duration: 30d
  google_oauth: true
"""
from __future__ import annotations

import secrets
import urllib.parse
from typing import Any

import requests
from django.conf import settings
from django.contrib.auth import authenticate, get_user_model, login, logout
from django.contrib.auth.hashers import make_password
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import ValidationError as _ValidationError
from django.core.mail import send_mail
from django.http import HttpResponseRedirect
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.encoding import force_bytes, force_str
from django.utils.http import (
    urlsafe_base64_decode, urlsafe_base64_encode, url_has_allowed_host_and_scheme,
)
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods

from .forms import LoginForm, SignupForm

User = get_user_model()


_LOGIN_LIMIT = 10        # max failed attempts per (ip, email-prefix) per window
_LOGIN_WINDOW_S = 900    # 15 minutes


def _client_ip(request):
    fwd = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")


def _login_throttle_key(request, email):
    return f"login_fail:{_client_ip(request)}:{(email or '').lower()[:64]}"


def login_view(request):
    from django.core.cache import cache
    next_url = request.GET.get("next") or request.POST.get("next") or "/home"
    # Open-redirect hardening: only allow same-site next targets.
    if not url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure(),
    ):
        next_url = "/home"
    error = None
    if request.method == "POST":
        form = LoginForm(request.POST)
        if form.is_valid():
            email = form.cleaned_data["email"].lower()
            password = form.cleaned_data["password"]
            # Bucket failed attempts per (ip, email). Defends both targeted
            # password-spray and brute-force against a single account. Locks
            # out the (ip, email) pair for the rest of the window after 10
            # failures — does NOT lock the user account globally, so a
            # genuine user from another IP can still get in.
            tkey = _login_throttle_key(request, email)
            if cache.get(tkey, 0) >= _LOGIN_LIMIT:
                error = "Too many failed attempts. Try again in 15 minutes."
                return render(request, "pharmacypulse/pages/login.html", {
                    "error": error, "next": next_url,
                    "google_oauth_enabled": bool(settings.GOOGLE_OAUTH_CLIENT_ID),
                })
            user = authenticate(request, username=email, password=password)
            if user and not user.deactivated_at:
                cache.delete(tkey)  # reset on successful login
                login(request, user)
                # Belt-and-suspenders: explicitly pin the session expiry to
                # SESSION_COOKIE_AGE so a previously short-lived session (e.g.
                # one created before SESSION_EXPIRE_AT_BROWSER_CLOSE was set)
                # gets refreshed to the full 30-day window.
                request.session.set_expiry(settings.SESSION_COOKIE_AGE)
                return redirect(next_url)
            try:
                cache.incr(tkey)
            except ValueError:
                cache.set(tkey, 1, timeout=_LOGIN_WINDOW_S)
            error = "Invalid email or password."
        else:
            error = "Please enter a valid email and password."
    return render(request, "pharmacypulse/pages/login.html", {
        "error": error, "next": next_url,
        "google_oauth_enabled": bool(settings.GOOGLE_OAUTH_CLIENT_ID),
    })


def signup_view(request):
    # Preserve the user's intended destination (e.g. returning to write the
    # review they started) across signup. Same-site hardening mirrors login_view.
    next_url = request.GET.get("next") or request.POST.get("next") or "/home"
    if not url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure(),
    ):
        next_url = "/home"
    email_prefill = (request.GET.get("email") or "").strip()[:254]
    error = None
    if request.method == "POST":
        # Throttle: max 5 signups per IP per hour. Above human use, blocks
        # bots farming throwaway accounts.
        from .views_extra import is_rate_limited
        if is_rate_limited(request, "signup", limit=5, window_s=3600):
            return render(request, "pharmacypulse/pages/signup.html", {
                "error": "Too many signup attempts. Try again in an hour.",
                "next": next_url, "email": email_prefill,
                "google_oauth_enabled": bool(settings.GOOGLE_OAUTH_CLIENT_ID),
            })
        form = SignupForm(request.POST)
        if form.is_valid():
            email = form.cleaned_data["email"]
            user = User(
                email=email, username=email,
                first_name=form.cleaned_data["first_name"],
                last_name=form.cleaned_data["last_name"],
                zip_code=form.cleaned_data.get("zip_code", ""),
                role="user",
            )
            # Enforce AUTH_PASSWORD_VALIDATORS (min length, common/numeric
            # password checks) — not just the form's len>=8.
            try:
                validate_password(form.cleaned_data["password"], user=user)
            except _ValidationError as exc:
                error = " ".join(exc.messages)
                return render(request, "pharmacypulse/pages/signup.html", {
                    "error": error,
                    "next": next_url,
                    "email": email,
                    "google_oauth_enabled": bool(settings.GOOGLE_OAUTH_CLIENT_ID),
                })
            user.set_password(form.cleaned_data["password"])
            user.save()
            # Newsletter opt-in. Form checkbox is `name="newsletter" value="1"`.
            # Mirror locally and push to the Resend audience so they receive
            # outbound campaigns going forward.
            if request.POST.get("newsletter") == "1":
                from .models import NewsletterSubscriber
                from . import resend_audience
                NewsletterSubscriber.objects.get_or_create(
                    email=user.email,
                    defaults={"user": user, "zip_code": user.zip_code or "",
                              "subscribed": 1},
                )
                resend_audience.add_contact(
                    email=user.email,
                    first_name=user.first_name or "",
                    last_name=user.last_name or "",
                )
            login(request, user)
            request.session.set_expiry(settings.SESSION_COOKIE_AGE)
            return redirect(next_url)
        if form.errors:
            field, errors = next(iter(form.errors.items()))
            label = form.fields[field].label or field.replace("_", " ").title()
            error = f"{label}: {errors[0]}"
        else:
            error = "Could not sign up."
    return render(request, "pharmacypulse/pages/signup.html", {
        "error": error,
        "next": next_url,
        "email": email_prefill,
        "google_oauth_enabled": bool(settings.GOOGLE_OAUTH_CLIENT_ID),
    })


@require_http_methods(["GET", "POST"])
def logout_view(request):
    logout(request)
    return redirect("/")


def forgot_password_view(request):
    sent = False
    error = None
    if request.method == "POST":
        # Throttle: 5 attempts per IP per hour. Stops bots farming the
        # endpoint to enumerate accounts (we already don't leak existence,
        # but the throttle is belt-and-suspenders).
        from .views_extra import is_rate_limited
        if is_rate_limited(request, "forgot_password", limit=5, window_s=3600):
            error = "Too many attempts. Try again in an hour."
        else:
            email = (request.POST.get("email") or "").lower().strip()
            # Always show "sent" UI regardless of account existence — we
            # don't leak which emails are registered. Actual delivery still
            # depends on DJANGO_EMAIL_BACKEND being a real provider (SendGrid
            # etc) on Heroku; with the console backend, the link is printed
            # to the dyno logs and the user sees nothing in their inbox.
            user = User.objects.filter(email__iexact=email, deactivated_at__isnull=True).first()
            if user:
                uid = urlsafe_base64_encode(force_bytes(user.pk))
                token = default_token_generator.make_token(user)
                base = settings.BRAND.get("url", "").rstrip("/") or request.build_absolute_uri("/").rstrip("/")
                link = f"{base}/reset-password/{uid}/{token}"
                try:
                    send_mail(
                        subject="Reset your PharmacyPulse password",
                        message=(
                            f"Hi {user.first_name or 'there'},\n\n"
                            f"Use this link to set a new password (valid for ~3 days):\n{link}\n\n"
                            f"If you didn't request this, you can ignore this email.\n\n"
                            f"— PharmacyPulse"
                        ),
                        from_email=settings.DEFAULT_FROM_EMAIL,
                        recipient_list=[user.email],
                        fail_silently=True,
                    )
                except Exception:
                    pass  # never block the "sent" UX on email-backend errors
            sent = True
    return render(request, "pharmacypulse/pages/forgot-password.html",
                  {"sent": sent, "error": error})


def reset_password_view(request, uidb64: str, token: str):
    error = None
    success = False
    user = None
    try:
        uid = force_str(urlsafe_base64_decode(uidb64))
        user = User.objects.filter(pk=uid).first()
    except (TypeError, ValueError, OverflowError):
        user = None
    valid = bool(user and default_token_generator.check_token(user, token))
    if not valid:
        error = "This reset link is invalid or has expired. Please request a new one."
    elif request.method == "POST":
        pw = request.POST.get("password") or ""
        pw2 = request.POST.get("password_confirm") or ""
        if pw != pw2:
            error = "Passwords do not match."
        else:
            try:
                validate_password(pw, user=user)
            except _ValidationError as exc:
                error = " ".join(exc.messages)
            else:
                user.set_password(pw)
                user.save()
                success = True
    return render(request, "pharmacypulse/pages/forgot-password.html",
                  {"reset_mode": True, "valid": valid, "error": error,
                   "success": success, "uid": uidb64, "token": token})


# --------------- Google OAuth (manual flow, no third-party dep) ------------

def google_start(request):
    if not settings.GOOGLE_OAUTH_CLIENT_ID:
        return redirect("/login?error=Google+sign-in+is+not+configured")
    state = secrets.token_urlsafe(24)
    request.session["google_oauth_state"] = state
    params = {
        "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
        "redirect_uri": request.build_absolute_uri(reverse("google_callback")),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    }
    return HttpResponseRedirect(
        "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)
    )


def google_callback(request):
    if request.GET.get("state") != request.session.pop("google_oauth_state", None):
        return redirect("/login?error=Invalid+OAuth+state")
    code = request.GET.get("code")
    if not code:
        return redirect("/login?error=Missing+OAuth+code")
    try:
        token_resp = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
                "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
                "redirect_uri": request.build_absolute_uri(reverse("google_callback")),
                "grant_type": "authorization_code",
            },
            timeout=15,
        )
        token_resp.raise_for_status()
        access_token = token_resp.json()["access_token"]
        info = requests.get(
            "https://openidconnect.googleapis.com/v1/userinfo",
            headers={"Authorization": f"Bearer {access_token}"}, timeout=15,
        ).json()
    except Exception:
        return redirect("/login?error=Google+sign-in+failed")
    email = (info.get("email") or "").lower()
    if not email:
        return redirect("/login?error=Google+did+not+return+an+email")
    sub = info.get("sub") or ""
    user = User.objects.filter(email__iexact=email).first()
    if user:
        if sub and not user.google_sub:
            user.google_sub = sub
            user.save(update_fields=["google_sub"])
    else:
        user = User(
            email=email, username=email,
            first_name=info.get("given_name", ""),
            last_name=info.get("family_name", ""),
            role="user", google_sub=sub,
            password=make_password(None),  # unusable password; OAuth-only
        )
        user.save()
    login(request, user)
    request.session.set_expiry(settings.SESSION_COOKIE_AGE)
    return redirect("/home")


# --------------- Facebook OAuth (manual flow, no third-party dep) ------------

def facebook_start(request):
    if not settings.FACEBOOK_OAUTH_APP_ID:
        return redirect("/login?error=Facebook+sign-in+is+not+configured")
    state = secrets.token_urlsafe(24)
    request.session["facebook_oauth_state"] = state
    redirect_uri = request.build_absolute_uri(reverse("facebook_callback"))
    params = {
        "client_id": settings.FACEBOOK_OAUTH_APP_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "email",
        "state": state,
    }
    return HttpResponseRedirect(
        "https://www.facebook.com/v18.0/dialog/oauth?" + urllib.parse.urlencode(params)
    )


def facebook_callback(request):
    if request.GET.get("state") != request.session.pop("facebook_oauth_state", None):
        return redirect("/login?error=Invalid+OAuth+state")
    code = request.GET.get("code")
    if not code:
        return redirect("/login?error=Missing+OAuth+code")
    redirect_uri = request.build_absolute_uri(reverse("facebook_callback"))
    try:
        token_resp = requests.get(
            "https://graph.facebook.com/v18.0/oauth/access_token",
            params={
                "client_id": settings.FACEBOOK_OAUTH_APP_ID,
                "client_secret": settings.FACEBOOK_OAUTH_APP_SECRET,
                "redirect_uri": redirect_uri,
                "code": code,
            },
            timeout=15,
        )
        token_resp.raise_for_status()
        access_token = token_resp.json()["access_token"]
        info = requests.get(
            "https://graph.facebook.com/v18.0/me",
            params={"fields": "id,email,first_name,last_name", "access_token": access_token},
            timeout=15,
        ).json()
    except Exception:
        return redirect("/login?error=Facebook+sign-in+failed")
    email = (info.get("email") or "").lower()
    if not email:
        return redirect("/login?error=Facebook+did+not+return+an+email")
    fb_id = info.get("id") or ""
    user = User.objects.filter(email__iexact=email).first()
    if user:
        if fb_id and not user.facebook_id:
            user.facebook_id = fb_id
            user.save(update_fields=["facebook_id"])
    else:
        user = User(
            email=email, username=email,
            first_name=info.get("first_name", ""),
            last_name=info.get("last_name", ""),
            role="user", facebook_id=fb_id,
            password=make_password(None),
        )
        user.save()
    login(request, user)
    request.session.set_expiry(settings.SESSION_COOKIE_AGE)
    return redirect("/home")
