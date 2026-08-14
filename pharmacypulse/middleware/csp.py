"""Set a Content-Security-Policy header on every HTML response.

The policy is intentionally permissive enough to keep the site working
(templates use heavy inline styles + inline event handlers) while blocking
arbitrary script injection from outside our trusted CDNs.

Trusted sources:
  - Google Maps JS SDK (maps.googleapis.com, maps.gstatic.com, ggpht.com)
  - Google Fonts (fonts.googleapis.com, fonts.gstatic.com)
  - Tailwind CDN (cdn.tailwindcss.com)
  - MarkerClusterer (unpkg.com)
  - Google Analytics gtag (googletagmanager.com, google-analytics.com)
  - IP geolocation lookup (ipapi.co)
  - Stripe checkout redirects (form-action)
"""


class ContentSecurityPolicyMiddleware:
    _CSP_BASE = (
        "default-src 'self'; "
        # 'unsafe-inline' is unavoidable: every page uses style="..." attrs
        # and inline <script> blocks for the homepage facts widget, ticker,
        # map init, etc. Migrating to nonces is a separate refactor.
        "script-src 'self' 'unsafe-inline' "
        "https://maps.googleapis.com https://maps.gstatic.com "
        "https://cdn.tailwindcss.com https://unpkg.com "
        "https://www.googletagmanager.com https://www.google-analytics.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com data:; "
        "img-src 'self' data: blob: "
        "https://maps.googleapis.com https://maps.gstatic.com "
        "https://*.ggpht.com https://*.google.com https://*.gstatic.com "
        "https://www.google-analytics.com; "
        "connect-src 'self' https://maps.googleapis.com https://maps.gstatic.com "
        "https://ipapi.co "
        "https://www.google-analytics.com https://region1.google-analytics.com "
        "https://region2.google-analytics.com https://analytics.google.com "
        "https://stats.g.doubleclick.net; "
        "frame-src 'self' https://js.stripe.com https://maps.google.com "
        "https://www.google.com; "
        "form-action 'self' https://checkout.stripe.com https://billing.stripe.com; "
        "base-uri 'self'; "
        "object-src 'none'"
    )

    # Pages that exist to be embedded on third-party sites need
    # frame-ancestors *; everywhere else clamps to 'none' to prevent
    # clickjacking. Modern browsers honor CSP frame-ancestors over
    # X-Frame-Options when both are present, so xframe_options_exempt on
    # the view alone isn't enough — the CSP has to lift the restriction too.
    _EMBEDDABLE_PATHS = ("/widget",)

    _CSP_FULL  = _CSP_BASE + "; frame-ancestors 'none'"
    _CSP_EMBED = _CSP_BASE + "; frame-ancestors *"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        # Only HTML responses get CSP — JSON APIs etc. don't render HTML so
        # the policy is meaningless for them.
        ctype = response.get("Content-Type", "")
        if "html" in ctype:
            embeddable = any(request.path.startswith(p) for p in self._EMBEDDABLE_PATHS)
            response["Content-Security-Policy"] = (
                self._CSP_EMBED if embeddable else self._CSP_FULL
            )
        return response