"""Operational endpoints + rate-limit helper."""
from __future__ import annotations

import hashlib

from django.core.cache import cache
from django.db import connection
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET

from .geo import client_ip


def is_rate_limited(request, action: str, *, limit: int = 20, window_s: int = 3600) -> bool:
    """Return True if the (IP, action) bucket has exceeded `limit` calls in
    the window. Increments the counter on each call. Same backing-cache
    pattern as the login throttle.

    Defaults: 20 per hour per IP — well above human use, blocks basic spam bots.
    """
    ip_hash = hashlib.sha256(client_ip(request).encode()).hexdigest()[:16]
    key = f"rl:{action}:{ip_hash}"
    current = cache.get(key)
    if current is None:
        cache.set(key, 1, timeout=window_s)
        return False
    if current >= limit:
        return True
    try:
        cache.incr(key)
    except ValueError:
        cache.set(key, 1, timeout=window_s)
    return False


@csrf_exempt
@require_GET
def healthcheck(request):
    """JSON ping that confirms DB connectivity. Heroku / uptime monitors poll
    this; failure → 503, lets Heroku auto-restart the dyno."""
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        return JsonResponse({"status": "ok", "db": "ok"}, status=200)
    except Exception as e:  # noqa: BLE001
        return JsonResponse({"status": "error", "db": str(e)[:200]}, status=503)
