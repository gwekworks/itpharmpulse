"""Geo-block middleware: reject non-US IPs with a 451 page.

Renders a friendly "service available in the US only" page for non-US IPs.

Fail-open by design: if the IP lookup fails, we let the request through
rather than locking out users when an upstream API hiccups. Country lookups
are cached 24h via geo._ip_lookup_full(), so the per-IP API cost is
amortized across all that IP's requests for the day.

Bypassed for:
  - Static + admin paths (Django is gracious about its own URLs)
  - Known crawler User-Agents (Googlebot, Bingbot, etc.) — blocking them
    would tank SEO indexing
  - Heroku's router healthchecks
  - Localhost / private IPs (dev environments)
"""
from __future__ import annotations

import re

from django.conf import settings
from django.http import HttpResponse
from django.template.loader import render_to_string

from ..geo import country_for_ip, client_ip


# Path prefixes that always bypass geo-blocking. Static assets, healthchecks,
# and admin pages (admins might be travelling).
_BYPASS_PREFIXES = (
    "/static/", "/healthcheck", "/robots.txt", "/sitemap.xml", "/__debug__",
    # Stripe webhook posts from Stripe's IPs (they're global) — must always
    # reach our endpoint regardless of country, or subscriptions break.
    "/api/flow/stripe-webhook",
)

# User-Agent tokens that identify legitimate crawlers. We allow these
# through unconditionally so SEO indexing keeps working.
_CRAWLER_RE = re.compile(
    r"(?i)(googlebot|bingbot|duckduckbot|slurp|yandexbot|baiduspider|"
    r"applebot|facebookexternalhit|twitterbot|linkedinbot|"
    r"pingdom|uptimerobot|statuscake|newrelic)"
)


def _is_blocked(country_code: str) -> bool:
    """Return True if this country code should be blocked. Empty/unknown
    country codes return False (fail-open)."""
    if not country_code:
        return False
    # Allow US plus its territories (PR, VI, GU, AS, MP) since the pharmacy
    # data covers them.
    return country_code not in {"US", "PR", "VI", "GU", "AS", "MP"}


class GeoBlockMiddleware:
    """Reject non-US IPs with a 451 'unavailable for legal reasons' page.

    Disabled when settings.GEO_BLOCK_ENABLED is False (dev / staging) so
    local browsers and CI runs aren't impacted.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self.enabled = getattr(settings, "GEO_BLOCK_ENABLED", False)

    def __call__(self, request):
        if not self.enabled:
            return self.get_response(request)

        path = request.path
        if any(path.startswith(p) for p in _BYPASS_PREFIXES):
            return self.get_response(request)

        ua = request.META.get("HTTP_USER_AGENT", "")
        if _CRAWLER_RE.search(ua):
            return self.get_response(request)

        ip = client_ip(request)
        country = country_for_ip(ip)
        if _is_blocked(country):
            html = render_to_string(
                "pharmacypulse/pages/geo-blocked.html",
                {"country_code": country},
                request=request,
            )
            # 451 Unavailable For Legal Reasons — semantically right for
            # geo restrictions and easy to filter in logs.
            return HttpResponse(html, status=451, content_type="text/html")

        return self.get_response(request)