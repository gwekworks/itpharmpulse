"""Shared decorators + helpers used by every flow module.

JWT ROADMAP (auth)
------------------
The app currently uses Django's server-side session auth for both SSR pages
and the /api/flow/* endpoints. Planned migration to JWT for the API surface:

- Issue short-lived (15m) access JWTs + rotating refresh tokens from
  auth_views (login/signup/Google OAuth) and a refresh endpoint.
- Serve refresh tokens as HttpOnly cookies; accept `Authorization: Bearer`
  for /api/flow/* calls from the SPA/API clients.
- Keep server-side sessions for SSR pages (template rendering + CSRF need
  them); do NOT rely on JWTs for cookie CSRF protection.
- Port `admin_required` / `login_required` below to JWT-backed authentication
  (decode + validate `sub` claim → user; role check unchanged). The endpoint
  handlers and their authorization checks stay identical either way.
- Add the JWT verification middleware after migration; gate it on a
  settings flag (e.g. AUTH_JWT_ENABLED) during the rollout.
"""
from __future__ import annotations

from django.http import HttpResponseForbidden
from django.shortcuts import redirect
from django.utils.http import url_has_allowed_host_and_scheme

from ...models import ActivityLog


def safe_next(request, fallback: str) -> str:
    """Return a same-site, safe redirect target from `next` (GET/POST) or
    `fallback`. Rejects off-site / scheme-relative URLs (open-redirect guard)."""
    candidate = request.POST.get("next") or request.GET.get("next") or fallback
    if not url_has_allowed_host_and_scheme(
        candidate, allowed_hosts={request.get_host()}, require_https=request.is_secure(),
    ):
        return fallback
    return candidate


def admin_required(view):
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect(f"/login?next={request.path}")
        if request.user.role != "admin":
            return HttpResponseForbidden("admin only")
        return view(request, *args, **kwargs)
    return wrapped


def _activity(request, type_, message):
    ActivityLog.objects.create(
        type=type_, message=message,
        user_id=request.user.id if request.user.is_authenticated else None,
    )
