"""Server-side proxy for Google Cloud Translation API v2.

Keeps the API key off the browser entirely. The client POSTs a JSON body
with the texts it wants translated; this view forwards them to Google and
returns the translated strings. sessionStorage caching on the client means
each unique page/language combo only hits this endpoint once per session.

Rate limiting: 60 requests per IP per minute — well above any real user
session but blocks trivial scraper abuse.

Supported targets: "es" (Spanish) and "en" (English / restore).
Translate to EN is essentially a no-op UI-side (the page reloads in EN) but
is supported here for completeness.
"""
from __future__ import annotations

import json
import logging

import requests
from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from ..flows.common import _activity

log = logging.getLogger(__name__)

_GOOGLE_TRANSLATE_URL = (
    "https://translation.googleapis.com/language/translate/v2"
)
_ALLOWED_TARGETS = {"es", "en"}
_MAX_TEXTS = 100       # Google v2 accepts up to 128 segments; we cap lower and batch internally
_MAX_CHARS = 10_000    # per request — prevents runaway translation of full-page HTML dumps
_RATE_LIMIT = 60       # requests per IP per minute


def _is_rate_limited(request) -> bool:
    """Simple in-process rate limit using Django's cache.
    Returns True when the caller should be blocked."""
    from django.core.cache import cache
    fwd = request.META.get("HTTP_X_FORWARDED_FOR", "")
    ip = fwd.split(",")[0].strip() if fwd else request.META.get("REMOTE_ADDR", "")
    key = f"translate_rl:{ip}"
    count = cache.get(key, 0)
    if count >= _RATE_LIMIT:
        return True
    cache.set(key, count + 1, timeout=60)
    return False


@csrf_exempt
@require_POST
def translate_texts(request):
    """POST /api/translate

    Request body (JSON):
        { "texts": ["Hello", "Find a pharmacy", ...], "target": "es" }

    Response (JSON):
        { "translations": ["Hola", "Encontrar una farmacia", ...] }

    Errors return { "error": "..." } with an appropriate HTTP status.
    """
    if not settings.GOOGLE_TRANSLATE_API_KEY:
        return JsonResponse({"error": "Translation service not configured"}, status=503)

    if _is_rate_limited(request):
        return JsonResponse({"error": "Too many requests"}, status=429)

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    texts = body.get("texts")
    target = (body.get("target") or "").strip().lower()

    if not texts or not isinstance(texts, list):
        return JsonResponse({"error": "texts must be a non-empty list"}, status=400)

    if target not in _ALLOWED_TARGETS:
        return JsonResponse(
            {"error": f"target must be one of {sorted(_ALLOWED_TARGETS)}"}, status=400
        )

    # Sanitise: drop empty strings, enforce char cap (batching handles segment count)
    texts = [str(t).strip() for t in texts if str(t).strip()]
    total_chars = sum(len(t) for t in texts)
    if total_chars > _MAX_CHARS:
        return JsonResponse({"error": "Request too large"}, status=400)

    if not texts:
        return JsonResponse({"translations": []})

    # If target is EN, just echo back — the client restores from its own cache.
    # We still handle it gracefully rather than hitting the API for a no-op.
    if target == "en":
        return JsonResponse({"translations": texts})

    try:
        # Google v2 hard limit is 128 segments per request — batch in chunks of 100
        _BATCH = 100
        all_translated = []
        for i in range(0, len(texts), _BATCH):
            chunk = texts[i:i + _BATCH]
            resp = requests.post(
                _GOOGLE_TRANSLATE_URL,
                params={"key": settings.GOOGLE_TRANSLATE_API_KEY},
                json={
                    "q": chunk,
                    "target": target,
                    "source": "en",
                    "format": "text",   # plain text — never HTML to avoid tag injection
                },
                timeout=8,
            )
            if resp.status_code != 200:
                log.warning(
                    "Google Translate returned %s: %s", resp.status_code, resp.text[:200]
                )
                try:
                    google_error = resp.json().get("error", {})
                    error_msg = google_error.get("message", f"HTTP {resp.status_code}")
                except Exception:
                    error_msg = f"HTTP {resp.status_code}: {resp.text[:100]}"
                return JsonResponse({"error": "Translation service error", "detail": error_msg}, status=502)
            try:
                data = resp.json()
                chunk_translated = [
                    item["translatedText"]
                    for item in data["data"]["translations"]
                ]
                all_translated.extend(chunk_translated)
            except (KeyError, ValueError, TypeError) as exc:
                log.warning("Unexpected Google Translate response shape: %s", exc)
                return JsonResponse({"error": "Unexpected response from translation service"}, status=502)

    except requests.RequestException as exc:
        log.warning("Google Translate request failed: %s", exc)
        return JsonResponse({"error": "Translation service unavailable"}, status=502)

    return JsonResponse({"translations": all_translated})
