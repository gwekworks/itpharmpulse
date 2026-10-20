"""Resend Audiences integration.

Wraps the contact-add / unsubscribe / delete endpoints so the rest of the
codebase doesn't need to know about Resend. All calls are best-effort —
they never raise into the caller (newsletter sync should not be able to
break signup). Errors are logged for ops to inspect.

API ref: https://resend.com/docs/api-reference/contacts/create-contact
"""
from __future__ import annotations

import logging
from typing import Optional

import requests
from django.conf import settings

log = logging.getLogger(__name__)

_BASE = "https://api.resend.com"
_TIMEOUT = 6  # seconds — never block a request handler for long


def _enabled() -> bool:
    return bool(getattr(settings, "RESEND_API_KEY", "") and
                getattr(settings, "RESEND_AUDIENCE_ID", ""))


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.RESEND_API_KEY}",
        "Content-Type": "application/json",
    }


def _audience_url(suffix: str = "") -> str:
    return f"{_BASE}/audiences/{settings.RESEND_AUDIENCE_ID}/contacts{suffix}"


def add_contact(email: str, first_name: str = "", last_name: str = "",
                unsubscribed: bool = False) -> Optional[str]:
    """Add or upsert a contact in the audience. Returns Resend's contact id
    on success or None on failure / when disabled."""
    if not _enabled() or not email:
        return None
    try:
        r = requests.post(
            _audience_url(),
            headers=_headers(),
            json={
                "email": email.strip().lower(),
                "first_name": (first_name or "")[:80],
                "last_name":  (last_name  or "")[:80],
                "unsubscribed": bool(unsubscribed),
            },
            timeout=_TIMEOUT,
        )
        if r.status_code in (200, 201):
            return (r.json() or {}).get("id")
        # 409 = already exists — that's success for us; we'll patch instead.
        if r.status_code == 409:
            return update_contact(email, unsubscribed=unsubscribed)
        log.warning("Resend add_contact %s: %s %s", email, r.status_code, r.text[:200])
    except requests.RequestException as e:
        log.warning("Resend add_contact %s failed: %s", email, e)
    return None


def update_contact(email: str, unsubscribed: Optional[bool] = None) -> Optional[str]:
    """Patch an existing contact (by email). Used for unsubscribe + resub."""
    if not _enabled() or not email:
        return None
    try:
        payload: dict = {}
        if unsubscribed is not None:
            payload["unsubscribed"] = bool(unsubscribed)
        r = requests.patch(
            f"{_audience_url()}/{email.strip().lower()}",
            headers=_headers(),
            json=payload,
            timeout=_TIMEOUT,
        )
        if r.status_code in (200, 204):
            return email
        log.warning("Resend update_contact %s: %s %s", email, r.status_code, r.text[:200])
    except requests.RequestException as e:
        log.warning("Resend update_contact %s failed: %s", email, e)
    return None


def unsubscribe(email: str) -> bool:
    """Mark a contact as unsubscribed. Equivalent to update_contact(..., True)."""
    return update_contact(email, unsubscribed=True) is not None


def delete_contact(email: str) -> bool:
    """Hard-delete a contact. Used for CCPA right-to-be-forgotten."""
    if not _enabled() or not email:
        return False
    try:
        r = requests.delete(
            f"{_audience_url()}/{email.strip().lower()}",
            headers=_headers(),
            timeout=_TIMEOUT,
        )
        if r.status_code in (200, 204, 404):  # 404 = already gone, treat as success
            return True
        log.warning("Resend delete_contact %s: %s %s", email, r.status_code, r.text[:200])
    except requests.RequestException as e:
        log.warning("Resend delete_contact %s failed: %s", email, e)
    return False
