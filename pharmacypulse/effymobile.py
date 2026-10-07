"""EffyMobile fax API client (login → JWT → send/detail/cancel).

Contract (frontend recipe):
  - POST {base}/api/auth/login  → {"message": ..., "token": "<jwt>"}
  - POST {base}/api/fax/send    → {"status": "success",
                                    "fax": {"id": <app uuid>, "fax_id": <telnyx>,
                                            "status": "queued", ...}}
  - GET  {base}/api/fax/{id}    → live-syncs non-final rows from Telnyx
  - DELETE {base}/api/fax/{id}  → cancels in-flight (no-op once final)

Auth config (env; missing creds => EffyDisabledError, callers degrade to
a friendly "fax not sent" note instead of crashing):
  - EFFYMOBILE_BASE_URL (legacy EFFYMOBILE key accepted as fallback)
  - EFFYMOBILE_EMAIL / EFFYMOBILE_PASSWORD

The JWT is cached in the Django cache until shortly before its `exp`
claim; a 401 clears the cache and retries once with a fresh login.
"""
from __future__ import annotations

import base64
import json
import logging
import time

import requests
from django.conf import settings
from django.core.cache import cache

log = logging.getLogger(__name__)

_TOKEN_CACHE_KEY = "effy:jwt"
_TIMEOUT_S = 20


class EffyDisabledError(RuntimeError):
    """EffyMobile is not configured (missing base URL or credentials)."""


class EffyApiError(RuntimeError):
    """EffyMobile answered with an error / unreachable."""


def base_url() -> str:
    base = (
        getattr(settings, "EFFYMOBILE_BASE_URL", "")
        or __import__("os").environ.get("EFFYMOBILE_BASE_URL", "")
        or __import__("os").environ.get("EFFYMOBILE", "")
    ).strip().rstrip("/")
    if not base:
        raise EffyDisabledError("EFFYMOBILE_BASE_URL is not set.")
    return base


def credentials() -> tuple[str, str]:
    import os
    email = (
        getattr(settings, "EFFYMOBILE_EMAIL", "")
        or os.environ.get("EFFYMOBILE_EMAIL", "")
        or os.environ.get("EFFYMOBILE_USERNAME", "")
    ).strip()
    password = (
        getattr(settings, "EFFYMOBILE_PASSWORD", "")
        or os.environ.get("EFFYMOBILE_PASSWORD", "")
    )
    if not (email and password):
        raise EffyDisabledError("EFFYMOBILE_EMAIL/PASSWORD are not set.")
    return email, password


def _jwt_exp(token: str) -> int:
    """Best-effort `exp` from the JWT payload (no signature verification —
    we only use it for cache TTL, the server is the authority)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return int(json.loads(base64.urlsafe_b64decode(payload)).get("exp", 0))
    except Exception:
        return 0


def login(force: bool = False) -> str:
    """Return a cached-or-fresh JWT, logging in when needed."""
    if not force:
        cached = cache.get(_TOKEN_CACHE_KEY)
        if cached:
            return cached
    email, password = credentials()
    url = f"{base_url()}/api/auth/login"
    try:
        resp = requests.post(
            url, json={"email": email, "password": password},
            timeout=_TIMEOUT_S,
        )
    except requests.RequestException as exc:
        raise EffyApiError(f"EffyMobile login unreachable: {exc}") from exc
    if resp.status_code in (400, 401, 422):
        # Some deployments expect `username` instead of `email` — retry once.
        try:
            resp = requests.post(
                url, json={"username": email, "password": password},
                timeout=_TIMEOUT_S,
            )
        except requests.RequestException as exc:
            raise EffyApiError(f"EffyMobile login unreachable: {exc}") from exc
    if resp.status_code >= 400:
        raise EffyApiError(
            f"EffyMobile login failed ({resp.status_code}): "
            f"{resp.text[:200]}"
        )
    try:
        token = resp.json().get("token", "")
    except ValueError as exc:
        raise EffyApiError("EffyMobile login returned non-JSON.") from exc
    if not token:
        raise EffyApiError("EffyMobile login returned no token.")
    ttl = max(_jwt_exp(token) - int(time.time()) - 300, 60)
    cache.set(_TOKEN_CACHE_KEY, token, timeout=min(ttl, 86400))
    return token


def _request(method: str, path: str, *, json_body=None,
             _retried: bool = False) -> dict:
    token = login()
    url = f"{base_url()}{path}"
    try:
        resp = requests.request(
            method, url,
            headers={"Authorization": f"Bearer {token}"},
            json=json_body, timeout=_TIMEOUT_S,
        )
    except requests.RequestException as exc:
        raise EffyApiError(f"EffyMobile {path} unreachable: {exc}") from exc
    if resp.status_code == 401 and not _retried:
        cache.delete(_TOKEN_CACHE_KEY)
        return _request(method, path, json_body=json_body, _retried=True)
    if resp.status_code >= 400:
        raise EffyApiError(
            f"EffyMobile {path} failed ({resp.status_code}): "
            f"{resp.text[:300]}"
        )
    try:
        data = resp.json()
    except ValueError as exc:
        raise EffyApiError(f"EffyMobile {path} returned non-JSON.") from exc
    return data if isinstance(data, dict) else {"data": data}


def normalize_us_fax(raw: str) -> str:
    """Free-text US number → E.164 (+1XXXXXXXXXX). Raises ValueError."""
    digits = "".join(c for c in (raw or "") if c.isdigit())
    if len(digits) == 10:
        digits = "1" + digits
    if len(digits) != 11 or not digits.startswith("1"):
        raise ValueError(f"Not a dialable US number: {raw!r}")
    return "+" + digits


def send_fax(to: str, media_url: str, **opts) -> dict:
    """Queue an outbound fax. Returns the `fax` object from the response."""
    payload = {"to": to, "media_url": media_url, "store_preview": True}
    for key in ("quality", "from_display_name", "t38_enabled",
                "monochrome", "store_media"):
        if key in opts and opts[key] is not None:
            payload[key] = opts[key]
    data = _request("POST", "/api/fax/send", json_body=payload)
    if data.get("status") != "success" or not isinstance(data.get("fax"), dict):
        raise EffyApiError(f"Unexpected send response: {str(data)[:300]}")
    return data["fax"]


def get_fax(fax_id: str) -> dict:
    """Live detail for one fax (server syncs non-final rows on read)."""
    data = _request("GET", f"/api/fax/{fax_id}")
    fax = data.get("fax", data)
    if not isinstance(fax, dict):
        raise EffyApiError(f"Unexpected detail response: {str(data)[:200]}")
    return fax


def cancel_fax(fax_id: str) -> dict:
    """Cancel an in-flight fax (no-op-safe once final)."""
    return _request("DELETE", f"/api/fax/{fax_id}")


def send_sms(to: str, text: str, media_urls=None) -> dict:
    """Send an SMS (fallback when a fax leg fails)."""
    payload = {"to": to, "text": text, "media_urls": media_urls or []}
    data = _request("POST", "/api/sms/send", json_body=payload)
    if data.get("status") == "error" or "error" in data:
        raise EffyApiError(f"SMS rejected: {str(data)[:300]}")
    return data


# EffyMobile → local status mapping.
STATUS_MAP = {
    "queued": "queued",
    "media.processed": "processing",
    "processing": "processing",
    "sending.started": "sending",
    "sending": "sending",
    "delivered": "delivered",
    "failed": "failed",
    "canceled": "canceled",
    "cancelled": "canceled",
}
