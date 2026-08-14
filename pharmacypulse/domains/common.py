"""Shared helpers + pharmacy/review/shortage row mappers.

Lives under domains/common so every page provider and flow module can
import the same dict-shapes without importing each other.
"""
from __future__ import annotations

import functools

from django.db.models import F, Q
from django.db.models.functions import ASin, Cos, Power, Radians, Sin, Sqrt

from ..models import DrugShortage, Pharmacy, Review
from ..text_utils import pharmacy_slug, strip_chain_suffix


def _safe_int(v):
    """Coerce a request.GET value to int or return None. Use anywhere we pass
    a user-supplied id straight to Pharmacy.objects.filter(id=...) etc. — the
    ORM is SQLi-safe (bound params) but raises ValueError on non-numeric input,
    bubbling up as a 500. None is treated as 'no id provided' by callers."""
    try:
        return int(str(v).strip()) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def published_pharmacies():
    """Public search queryset — rows from pharmacies_publishable that are
    actually published. Public browse pages query this instead of the live
    pharmacies table so moderation (publish_status) gates what the public
    sees without touching API-synced data."""
    from ..models import PharmacyPublishable
    return PharmacyPublishable.objects.filter(
        publish_status="published", status="active")


def with_get_params(fn):
    """Expose every request.GET key as a top-level `param_X` context var so
    templates' `{% if param_status == "current" %}` selectors light up.

    Replaces the old `with_prod_queries` decorator, which used to also run a
    catalog of hand-written SQL blocks per page. After the ORM migration that
    catalog is empty — only the param-injection side-effect is still useful."""
    @functools.wraps(fn)
    def wrapper(request, *args, **kwargs):
        ctx = {f"param_{k}": v for k, v in request.GET.items()}
        ctx.update(fn(request, *args, **kwargs) or {})
        return ctx
    return wrapper


class Row(dict):
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)


def _distance_mi_expr(olat, olng, lat_field="latitude", lng_field="longitude"):
    """SQL expression computing great-circle distance (miles) from a fixed
    origin to each row's latitude/longitude via the haversine formula.

    Computed entirely in SQL (ACOS/COS/SIN/SQRT) so ordering happens before
    the LIMIT slice — no PostGIS required. Rows without lat/lng yield NULL
    and sort last under ASC ordering. The coordinate columns are
    parameterizable so callers can measure against annotated COALESCE
    columns (e.g. pharmacy coordinates falling back to ZIP centroids)."""
    R = 3959.0
    lat1 = Radians(olat)
    lng1 = Radians(olng)
    dlat = (Radians(F(lat_field)) - lat1) / 2.0
    dlon = (Radians(F(lng_field)) - lng1) / 2.0
    a = (Power(Sin(dlat), 2)
         + Cos(lat1) * Cos(Radians(F(lat_field))) * Power(Sin(dlon), 2))
    return 2 * R * ASin(Sqrt(a))


def _zip_city_state(zip_code):
    """Resolve a 5-digit ZIP to its (city, state) name via the ZipCentroid
    cache filled during Google geocoding. Returns None when unknown. Used to
    convert a digit-only ZIP query into a location NAME we can match against —
    our NPPES pharmacy rows carry city/state but often no ZIP."""
    if not zip_code or len(zip_code) != 5:
        return None
    from ..models import ZipCentroid
    zc = ZipCentroid.objects.filter(zip=zip_code).first()
    if zc and (zc.city or zc.state):
        return zc.city, zc.state
    return None


_TAXONOMY_BADGE = {
    "3336C0003X": "Retail",
    "3336M0002X": "Mail Order",
    "3336S0011X": "Specialty",
    "3336C0004X": "Compounding",
    "3336H0001X": "Home Infusion",
    "3336L0003X": "Long-Term Care",
    "3336N0007X": "Nuclear",
    "3336I0012X": "Institutional",
}


def _pharmacy_dict(p: Pharmacy) -> dict:
    # Service badges from primary + secondary taxonomies.
    badges = []
    if p.taxonomy_code and p.taxonomy_code in _TAXONOMY_BADGE:
        badges.append(_TAXONOMY_BADGE[p.taxonomy_code])
    for code in (p.secondary_taxonomies or "").split(","):
        code = code.strip()
        if code and code in _TAXONOMY_BADGE and _TAXONOMY_BADGE[code] not in badges:
            badges.append(_TAXONOMY_BADGE[code])

    # Strip per-location store-number suffix for display ('CVS PHARMACY #
    # 08910' → 'CVS PHARMACY'). Original name is preserved in the DB; this
    # is display-only.
    from ..text_utils import strip_chain_suffix, pharmacy_slug
    display_name = strip_chain_suffix(p.name) or p.name
    slug = pharmacy_slug(p.name)
    detail_url = f"/pharmacy/{p.id}/{slug}/"

    return {
        "id": p.id, "name": display_name, "address": p.address, "city": p.city,
        "state": p.state, "zip": p.zip, "phone": p.phone or "",
        "slug": slug, "url": detail_url,
        "latitude": p.latitude, "longitude": p.longitude,
        "stock_confidence": p.stock_confidence,
        "avg_wait_time": p.avg_wait_time,
        "avg_service_rating": p.avg_service_rating,
        "total_reviews": p.total_reviews,
        "is_digital": p.is_digital, "website": p.website or "",
        "npi_number": p.npi_number or "",
        "claimed_by": p.claimed_by, "org_id": p.org_id,
        "status": p.status,
        "phone_wait_time": p.phone_wait_time,
        "delivery_wait_time": p.delivery_wait_time,
        "delivery_rating": p.delivery_rating,
        # NPPES-derived display fields. Available on every card now, not just
        # the detail page.
        "npi_verified": bool(p.npi_number and p.npi_number.isdigit() and len(p.npi_number) == 10),
        # Owner finished the "Complete your listing" checklist → earns the
        # persistent Verified badge shown next to the pharmacy name.
        "listing_verified": bool(getattr(p, "listing_completed_at", None)),
        "established_year": p.enumeration_date.year if p.enumeration_date else None,
        "service_badges": badges,
        # Distance sort sets this annotation on the queryset; absent elsewhere.
        "distance_mi": getattr(p, "distance_mi", None),
        # Chain detection: is_chain True if this DBA name appears on 5+ rows.
        # 'Independent' if chain_size <= 1, 'Chain · X locations' otherwise.
        "chain_size": p.chain_size or 1,
        "is_chain": (p.chain_size or 1) >= 5,
    }


def _review_dict(r: Review) -> dict:
    from ..text_utils import pharmacy_slug
    p_url = (f"/pharmacy/{r.pharmacy_id}/{pharmacy_slug(r.pharmacy_name or '')}/"
             if r.pharmacy_id else "")
    return {
        "id": r.id, "pharmacy_id": r.pharmacy_id, "pharmacy_name": r.pharmacy_name,
        "pharmacy_url": p_url,
        "user_id": r.user_id, "user_name": r.user_name,
        "stock_available": r.stock_available,
        "wait_time_rating": r.wait_time_rating,
        "service_rating": r.service_rating,
        "comment": r.comment or "",
        "is_caregiver": r.is_caregiver,
        "response_text": r.response_text or "",
        "response_date": r.response_date or "",
        "moderation_status": r.moderation_status,
        "created_at": r.created_at,
    }


def _shortage_dict(s: DrugShortage) -> dict:
    return {
        "id": s.id, "drug_name": s.drug_name, "generic_name": s.generic_name or "",
        "manufacturer": s.manufacturer or "", "status": s.status,
        "reason": s.reason or "", "estimated_resolution": s.estimated_resolution or "",
        "alternatives": s.alternatives or "", "updated_at": s.updated_at,
    }


def _normalize_validation_error(raw: str) -> str:
    # Some older error URLs in the wild captured str(ValidationError(...)),
    # which renders the message wrapped as ['msg']. Strip the list literal
    # so the user-facing banner reads cleanly. Multi-message lists get joined.
    if not raw:
        return raw
    s = raw.strip()
    if s.startswith("[") and s.endswith("]"):
        try:
            import ast
            v = ast.literal_eval(s)
            if isinstance(v, (list, tuple)):
                return "; ".join(str(x) for x in v)
        except (ValueError, SyntaxError):
            pass
    return raw


def _compare_winners(items):
    # Per-metric winner among items that have at least one review. Empty when
    # fewer than 2 rated entries, since "winning" needs something to beat.
    rated = [x for x in items if x.get("total_reviews")]
    if len(rated) < 2:
        return {}
    return {
        "stock":   max(rated, key=lambda x: x["stock_confidence"])["id"],
        "wait":    min(rated, key=lambda x: x["avg_wait_time"])["id"],
        "rating":  max(rated, key=lambda x: x["avg_service_rating"])["id"],
        "reviews": max(rated, key=lambda x: x["total_reviews"])["id"],
    }


_STATE_NAMES = {
    "AL":"Alabama","AK":"Alaska","AZ":"Arizona","AR":"Arkansas","CA":"California",
    "CO":"Colorado","CT":"Connecticut","DE":"Delaware","FL":"Florida","GA":"Georgia",
    "HI":"Hawaii","ID":"Idaho","IL":"Illinois","IN":"Indiana","IA":"Iowa",
    "KS":"Kansas","KY":"Kentucky","LA":"Louisiana","ME":"Maine","MD":"Maryland",
    "MA":"Massachusetts","MI":"Michigan","MN":"Minnesota","MS":"Mississippi",
    "MO":"Missouri","MT":"Montana","NE":"Nebraska","NV":"Nevada","NH":"New Hampshire",
    "NJ":"New Jersey","NM":"New Mexico","NY":"New York","NC":"North Carolina",
    "ND":"North Dakota","OH":"Ohio","OK":"Oklahoma","OR":"Oregon","PA":"Pennsylvania",
    "RI":"Rhode Island","SC":"South Carolina","SD":"South Dakota","TN":"Tennessee",
    "TX":"Texas","UT":"Utah","VT":"Vermont","VA":"Virginia","WA":"Washington",
    "WV":"West Virginia","WI":"Wisconsin","WY":"Wyoming","DC":"D.C.","PR":"Puerto Rico",
}


BRAND_PATTERNS = [
    ("walgreens",   "Walgreens",      Q(name__istartswith="walgreens")),
    ("cvs",         "CVS",            Q(name__istartswith="cvs ") | Q(name__iexact="cvs") | Q(name__istartswith="cvs/")),
    ("walmart",     "Walmart",        Q(name__istartswith="walmart")),
    ("rite_aid",    "Rite Aid",       Q(name__istartswith="rite aid")),
    ("kroger",      "Kroger",         Q(name__istartswith="kroger")),
    ("publix",      "Publix",         Q(name__istartswith="publix")),
    ("costco",      "Costco",         Q(name__istartswith="costco")),
    ("sams_club",   "Sam's Club",     Q(name__istartswith="sam's club") | Q(name__istartswith="sams club")),
    ("albertsons",  "Albertsons",     Q(name__istartswith="albertsons")),
    ("safeway",     "Safeway",        Q(name__istartswith="safeway")),
    ("target",      "Target",         Q(name__istartswith="target")),
    ("meijer",      "Meijer",         Q(name__istartswith="meijer")),
    ("hyvee",       "Hy-Vee",         Q(name__istartswith="hy-vee") | Q(name__istartswith="hyvee")),
    ("wegmans",     "Wegmans",        Q(name__istartswith="wegmans")),
    ("heb",         "H-E-B",          Q(name__istartswith="h-e-b") | Q(name__istartswith="heb pharmacy")),
]


BRAND_PATTERNS_BY_KEY = {key: (label, q) for key, label, q in BRAND_PATTERNS}


DEFUNCT_CHAIN_KEYS = {"rite_aid"}


_TAXONOMY_LABEL = {
    "3336C0003X": "Retail",
    "3336M0002X": "Mail order",
    "3336S0011X": "Specialty",
    "3336C0004X": "Compounding",
    "3336L0003X": "Long-term care",
    "3336H0001X": "Home infusion",
    "3336I0012X": "Institutional",
    "3336N0007X": "Nuclear",
}
