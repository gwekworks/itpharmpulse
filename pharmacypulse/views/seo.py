"""SEO/ops views: robots.txt + sitemap."""
from __future__ import annotations

from django.conf import settings
from django.http import HttpResponse

from ..models import Pharmacy


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


def sitemap_xml(request):
    """Google sitemap. The pharmacy <url> entries are computed from a
    79k-row table, so the full document is cached for 24h in the shared
    cache (DatabaseCache in prod). Without this every crawl of the sitemap
    re-scans the pharmacies table.

    Also capped at 10k pharmacy URLs per sitemap spec limit (50k URLs,
    50MB) and self-consistent — URL list + metadata all come from one
    cached build.
    """
    from xml.sax.saxutils import escape as xml_escape
    from django.core.cache import cache
    site = settings.BRAND.get("url", "") or request.build_absolute_uri("/").rstrip("/")

    _SITEMAP_CACHE_KEY = "seo:sitemap:v2"
    cached = cache.get(_SITEMAP_CACHE_KEY)
    if cached:
        return HttpResponse(cached, content_type="application/xml",
                            headers={"Cache-Control": "public, max-age=3600"})

    from ..text_utils import pharmacy_slug
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    # Static high-priority pages with changefreq + priority hints — helps Google
    # decide how often to recrawl and which pages to feature.
    static_pages = [
        ("/",            "daily",   "1.0"),
        ("/list",        "daily",   "0.9"),
        ("/map",         "weekly",  "0.8"),
        ("/online",      "weekly",  "0.8"),
        # ("/shortages",   "daily",   "0.8"),
        ("/reviews",     "daily",   "0.7"),
        ("/compare",     "weekly",  "0.6"),
        ("/for-pharmacies", "monthly", "0.5"),
        ("/privacy",     "yearly",  "0.3"),
        ("/terms",       "yearly",  "0.3"),
    ]
    for path, freq, prio in static_pages:
        parts.append(
            f"<url><loc>{site}{path}</loc>"
            f"<changefreq>{freq}</changefreq>"
            f"<priority>{prio}</priority></url>"
        )
    # Dynamic pharmacy pages — keyword slugs in the URL so Google indexes them
    # for "pharmacy near {name}" queries.
    for pid, name, updated in (Pharmacy.objects.filter(status="active", deleted_at__isnull=True)
                               .values_list("id", "name", "updated_at")[:10000]):
        slug = pharmacy_slug(name)
        loc = f"{site}/pharmacy/{pid}/{slug}/"
        lastmod = updated.date().isoformat() if updated else ""
        parts.append(
            f"<url><loc>{xml_escape(loc)}</loc>"
            + (f"<lastmod>{lastmod}</lastmod>" if lastmod else "")
            + "<changefreq>weekly</changefreq><priority>0.5</priority></url>"
        )
    parts.append("</urlset>")
    body = "\n".join(parts)
    cache.set(_SITEMAP_CACHE_KEY, body, 24 * 3600)
    return HttpResponse(body, content_type="application/xml",
                        headers={"Cache-Control": "public, max-age=3600"})