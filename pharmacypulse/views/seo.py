"""SEO/ops views: robots.txt + sitemap."""
from __future__ import annotations

from django.conf import settings
from django.http import HttpResponse


def robots_txt(request):
    site = settings.BRAND.get("url", "") or request.build_absolute_uri("/").rstrip("/")
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /django-admin/\n"
        "Disallow: /admin-\n"
        "Disallow: /api/\n"
        "Disallow: /account\n"
        "Disallow: /home\n"
        "Disallow: /pharmacist-dashboard\n"
        "Disallow: /login\n"
        "Disallow: /signup\n"
        "Disallow: /forgot-password\n"
        "Disallow: /reset-password/\n"
        f"\nSitemap: {site}/sitemap.xml\n"
    )
    return HttpResponse(body, content_type="text/plain",
                        headers={"Cache-Control": "public, max-age=86400"})


# Static public pages with changefreq + priority hints — helps Google decide
# how often to recrawl and which pages to feature.
#
# Individual pharmacy pages (/pharmacy/<id>/<slug>/) are intentionally NOT
# listed. They stay crawlable (robots.txt allows them) and Google can still
# reach them through internal links from /list, /reviews, etc.
_SITEMAP_PAGES = [
    ("/",            "daily",   "1.0"),
    ("/list",        "daily",   "0.9"),
    # ("/map",         "weekly",  "0.8"),  # route hidden — returns 404
    ("/online",      "weekly",  "0.8"),
    # ("/shortages",   "daily",   "0.8"),
    ("/reviews",     "daily",   "0.7"),
    ("/compare",     "weekly",  "0.6"),
    ("/for-pharmacies", "monthly", "0.5"),
    ("/about",       "monthly", "0.5"),
    # ("/insights",    "weekly",  "0.6"),  # route hidden — returns 404
    ("/privacy",     "yearly",  "0.3"),
    ("/terms",       "yearly",  "0.3"),
]


def sitemap_xml(request):
    """Google sitemap of the static public pages. No DB access, so no
    server-side cache is needed; the Cache-Control header lets crawlers and
    CDNs reuse it for an hour."""
    site = settings.BRAND.get("url", "") or request.build_absolute_uri("/").rstrip("/")
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for path, freq, prio in _SITEMAP_PAGES:
        parts.append(
            f"<url><loc>{site}{path}</loc>"
            f"<changefreq>{freq}</changefreq>"
            f"<priority>{prio}</priority></url>"
        )
    parts.append("</urlset>")
    return HttpResponse("\n".join(parts), content_type="application/xml",
                        headers={"Cache-Control": "public, max-age=3600"})
