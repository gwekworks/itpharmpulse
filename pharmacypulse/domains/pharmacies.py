"""Pharmacy browse providers: list, map, online, detail, compare, claim, widget."""
from __future__ import annotations

import functools
import re
from datetime import timedelta

from django.core.cache import cache
from django.db.models import Case, Count, F, Q, Sum, Value, When
from django.utils import timezone as djtz

from ..models import (
    ActivityLog, DataRequest, DrugShortage, ModerationKeyword,
    NewsletterSubscriber, Notification, Pharmacy, PharmacyClaim, PharmacyHours,
    PharmacyOrg, PharmacyTeamMember, ResponseCount, Review, ReviewResponse,
    SavedComparison, User, UserConsent,
)

from .common import (
    Row, with_get_params, _safe_int, _pharmacy_dict, _review_dict,
    _shortage_dict, _compare_winners, _normalize_validation_error,
    _distance_mi_expr, _zip_city_state, _TAXONOMY_BADGE, _TAXONOMY_LABEL, _STATE_NAMES,
    BRAND_PATTERNS, BRAND_PATTERNS_BY_KEY, DEFUNCT_CHAIN_KEYS,
    published_pharmacies,
)


def _resolve_origin(auto_loc, *zip_hints):
    """Best-effort reference point for distance sorting. Prefers explicit
    lat/lng on the resolved location (params, cookie, IP) — otherwise falls
    back to the ZIP centroid of any provided ZIP (filter ZIP, searched ZIP,
    resolved ZIP). Returns (lat, lng) or (None, None)."""
    from ..models import ZipCentroid

    if auto_loc.lat is not None and auto_loc.lng is not None:
        return auto_loc.lat, auto_loc.lng
    for hint in zip_hints:
        z = (hint or "").strip()
        if z:
            cen = ZipCentroid.objects.filter(zip=z[:5]).first()
            if cen:
                return cen.latitude, cen.longitude
    return None, None


@with_get_params
def page_list(request):
    from ..geo import resolve as resolve_loc
    from ..models import ZipCentroid
    q = (request.GET.get("q") or "").strip()
    city = (request.GET.get("city") or "").strip()
    zip_filter = (request.GET.get("zip") or "").strip()
    service = (request.GET.get("service") or "").strip()
    sort = (request.GET.get("sort") or "").strip()

    # Normalize a ZIP-like query ("60601", "60601-1234", " 60601 ") → the
    # leading 5 digits, so ZIP searches work regardless of formatting.
    zip_query = re.sub(r"\D", "", q)[:5] if q else ""

    # Auto-localize when the user hasn't explicitly searched.
    # Resolution chain: explicit ?zip → authed user.zip_code → IP postal → IP lat/lng.
    # ?show_all=1 explicitly opts out — used by the "Show all" link in the
    # auto-loc banner so users can escape localization without typing a query.
    show_all = request.GET.get("show_all") == "1"
    auto_loc = resolve_loc(request)
    if not (q or zip_filter or city or show_all):
        if auto_loc.zip:
            zip_filter = auto_loc.zip

    # Reference point for closest-first sorting (lat/lng from the resolved
    # location, falling back to the ZIP centroid when only a ZIP is known —
    # the filter ZIP, a searched 5-digit ZIP, or the auto-resolved ZIP).
    search_zip = zip_query if len(zip_query) == 5 else ""
    origin_lat, origin_lng = _resolve_origin(auto_loc, zip_filter, search_zip, auto_loc.zip)

    qs = published_pharmacies()
    if q:
        # Search across name/city/address AND ZIP. Without the zip clause a
        # search like '60601' (Chicago downtown) returned nothing — neither
        # name nor city contains the digits, and our NPPES addresses don't
        # always include the full ZIP in the address line.
        q_filter = Q(name__icontains=q) | Q(city__icontains=q) | Q(address__icontains=q)
        if len(zip_query) >= 3:
            q_filter |= Q(zip__startswith=zip_query)
        # Full 5-digit ZIP → resolve it to a location NAME first, then match
        # pharmacies within that location. NPPES rows often have no ZIP, so a
        # digit-only clause alone returns nothing.
        if len(zip_query) == 5:
            zc = (ZipCentroid.objects.filter(zip=zip_query).first()
                  if zip_query else None)
            if zc and zc.city:
                # "within that location name" — city+state match. iexact handles
                # NPPES "CHICAGO" vs Google "Chicago".
                q_filter |= Q(city__iexact=zc.city, state__iexact=zc.state)
            # Centroid radius from ZipCentroid's own coords (not Pharmacy.zip,
            # which is empty) still catches pharmacies just over a town/zip
            # border, and covers ZIPs whose centroid has no city/state yet.
            if zc and zc.latitude and zc.longitude:
                # ~0.072° ≈ 5 miles latitude; lng pad widened to ~0.095° to
                # cover cos(lat) shrinkage across CONUS (33°–48°N).
                lat, lng = zc.latitude, zc.longitude
                q_filter |= (Q(latitude__gte=lat - 0.075, latitude__lte=lat + 0.075,
                               longitude__gte=lng - 0.095, longitude__lte=lng + 0.095))
        qs = qs.filter(q_filter)
    if city:
        # Hero search maps the location field to ?city=. Beyond the exact city
        # match, also surface pharmacies whose name or address contains the
        # location word — a search for "Chicago" should catch a pharmacy named
        # "Chicago Medical Pharmacy" in a neighboring town, and partial names
        # like "Chicago Heights" match rows iexact can't.
        qs = qs.filter(
            Q(city__icontains=city)
            | Q(name__icontains=city)
            | Q(address__icontains=city)
        )
    if zip_filter:
        # ?zip= param (or the user's profile / detected ZIP) — resolve a full
        # ZIP to its city name too, so it works even though pharmacies don't
        # store ZIPs.
        zc = _zip_city_state(zip_filter[:5])
        if zc:
            city_name, state_code = zc
            exact_q = Q(city__iexact=city_name, state__iexact=state_code)
            if qs.filter(exact_q).exists():
                # Exact city match — e.g. "60601 → Chicago, IL".
                qs = qs.filter(exact_q)
            else:
                # No pharmacies in that city (sparse NPPES coverage / small
                # town) — widen to a ~10-15 mi radius around the ZIP centroid
                # so the page doesn't read "No pharmacies found".
                cen = ZipCentroid.objects.filter(zip=zip_filter[:5]).first()
                if cen and cen.latitude and cen.longitude:
                    lat, lng = cen.latitude, cen.longitude
                    radius_q = Q(
                        latitude__gte=lat - 0.15, latitude__lte=lat + 0.15,
                        longitude__gte=lng - 0.2, longitude__lte=lng + 0.2)
                    if qs.filter(radius_q).exists():
                        qs = qs.filter(radius_q)
                # Otherwise leave localization off → show the global list.
        else:
            # Partial ZIP / unresolvable → match the same ZIP-3 prefix area,
            # dropping the constraint if it would return an empty page.
            prefix_q = Q(zip__startswith=zip_filter[:3])
            if qs.filter(prefix_q).exists():
                qs = qs.filter(prefix_q)
    elif not (q or city or show_all) and auto_loc and auto_loc.lat and auto_loc.lng:
        # We have a lat/lng but no clean ZIP from the IP lookup (some IPs lack
        # postal_code in ipapi.co's response). Fall back to a ~50-mile bounding-
        # box filter — far more useful than rendering an arbitrary global list.
        # 0.7° latitude ≈ 48mi; 0.7° longitude ≈ 48mi at 30°N (US south) and
        # ~40mi at 45°N (US north). Good enough for "pharmacies near me" UX.
        lat, lng = auto_loc.lat, auto_loc.lng
        bbox_qs = qs.filter(
            latitude__gte=lat - 0.7, latitude__lte=lat + 0.7,
            longitude__gte=lng - 0.7, longitude__lte=lng + 0.7,
        )
        # Only use the bbox if it returns enough pharmacies; otherwise widen
        # back to global so we don't render an empty page.
        if bbox_qs.count() >= 5:
            qs = bbox_qs
    # Service filter — driven by NPPES taxonomy code. Each option matches
    # both the primary taxonomy_code and the secondary_taxonomies CSV, so
    # a Community/Retail pharmacy that ALSO does compounding shows up under
    # both "Retail" and "Compounding".
    # Default: hide our curated digital/mail-order rows ("Walgreens (Online)",
    # "Walmart Pharmacy", etc) from the general list. Tester feedback was that
    # those entries look like the physical chain is mislabeled as online.
    # They still appear under ?service=online and on /online.
    if not service:
        qs = qs.filter(is_digital=0)
    if service == "retail":
        qs = qs.filter(Q(taxonomy_code="3336C0003X") | Q(secondary_taxonomies__icontains="3336C0003X"))
    elif service in ("online", "mail_order", "mail"):
        # Mail-order pharmacies (taxonomy 3336M0002X) are also flagged
        # is_digital=1 by the sync — match either to be safe.
        qs = qs.filter(Q(is_digital=True) | Q(taxonomy_code="3336M0002X")
                       | Q(secondary_taxonomies__icontains="3336M0002X"))
    elif service == "specialty":
        qs = qs.filter(Q(taxonomy_code="3336S0011X") | Q(secondary_taxonomies__icontains="3336S0011X"))
    elif service == "compounding":
        qs = qs.filter(Q(taxonomy_code="3336C0004X") | Q(secondary_taxonomies__icontains="3336C0004X"))
    elif service == "long_term_care":
        qs = qs.filter(Q(taxonomy_code="3336L0003X") | Q(secondary_taxonomies__icontains="3336L0003X"))
    elif service == "home_infusion":
        qs = qs.filter(Q(taxonomy_code="3336H0001X") | Q(secondary_taxonomies__icontains="3336H0001X"))

    # Ownership filter — chain_size tier.
    ownership = (request.GET.get("ownership") or "").strip()
    if ownership == "independent":
        qs = qs.filter(chain_size__lt=5)
    elif ownership == "chain":
        qs = qs.filter(chain_size__gte=5, chain_size__lt=100)
    elif ownership == "big_chain":
        qs = qs.filter(chain_size__gte=100)

    # Has-reviews toggle — limits results to pharmacies users have actually
    # rated. Useful when 99% of NPPES rows have 0 reviews and the user wants
    # to skim 'community-vetted' pharmacies.
    if request.GET.get("has_reviews") == "1":
        qs = qs.filter(total_reviews__gt=0)

    # Era filter — pharmacy age via NPPES enumeration_date. Buckets match
    # decade boundaries with a 'recent' (2020+) for newest entrants.
    era = (request.GET.get("era") or "").strip()
    if era == "recent":
        qs = qs.filter(enumeration_date__gte="2020-01-01")
    elif era == "2010s":
        qs = qs.filter(enumeration_date__gte="2010-01-01", enumeration_date__lt="2020-01-01")
    elif era == "2000s":
        qs = qs.filter(enumeration_date__gte="2000-01-01", enumeration_date__lt="2010-01-01")
    elif era == "pre2000":
        qs = qs.filter(enumeration_date__lt="2000-01-01")

    sort_map = {
        "distance": "distance_mi",
        "reviews": "-total_reviews",
        "stock":   "-stock_confidence",
        "wait":    "avg_wait_time",
        "service": "-avg_service_rating",
        "name":    "name",
    }
    # When we have a reference point, the default sort is closest-first.
    # Otherwise fall back to highest rated first (tiebroken on review count).
    if origin_lat is not None and origin_lng is not None:
        from django.db.models import OuterRef, Subquery
        from django.db.models.functions import Coalesce
        from ..models import ZipCentroid
        # Most NPPES rows lack coordinates; fall back to the ZIP centroid so
        # distances render (and sort) for the full result set, not just the
        # few enriched rows.
        cen_lat = Subquery(
            ZipCentroid.objects.filter(zip=OuterRef("zip")).values("latitude")[:1])
        cen_lng = Subquery(
            ZipCentroid.objects.filter(zip=OuterRef("zip")).values("longitude")[:1])
        qs = qs.annotate(
            eff_lat=Coalesce(F("latitude"), cen_lat),
            eff_lng=Coalesce(F("longitude"), cen_lng),
        ).annotate(distance_mi=_distance_mi_expr(origin_lat, origin_lng, "eff_lat", "eff_lng"))
    elif sort == "distance":
        sort = ""  # no reference point → the distance annotation doesn't exist
    primary = sort_map.get(sort) or ("distance_mi" if origin_lat is not None else "-avg_service_rating")
    qs = qs.order_by(primary, "-total_reviews", "pk")

    pharmacies = [_pharmacy_dict(p) for p in qs[:200]]
    most_reviewed = [_pharmacy_dict(p) for p in
                     published_pharmacies().filter(is_digital=0, total_reviews__gt=0)
                     .order_by("-total_reviews")[:6]]
    saved_ids = []
    if request.user.is_authenticated:
        saved_ids = list(SavedComparison.objects.filter(
            user=request.user).values_list("pharmacy_id", flat=True))
    return {
        "pharmacies": pharmacies,
        "most_reviewed": most_reviewed,
        "total_count": Row({"total": len(pharmacies)}),
        "q": q, "city": city, "zip": zip_filter,
        "service": service, "sort": sort,
        "saved_ids": saved_ids,
        # Tells the template whether to show "near {zip} (auto-detected)" copy.
        "auto_loc_zip":    auto_loc.zip    if auto_loc else "",
        "auto_loc_city":   auto_loc.city   if auto_loc else "",
        "auto_loc_state":  auto_loc.state  if auto_loc else "",
        "auto_loc_source": auto_loc.source if auto_loc else "",
        # True when a reference point (browser / IP / ZIP centroid) exists and
        # closest-first sorting is actually in play.
        "sorted_by_distance": origin_lat is not None and origin_lng is not None,
        # Render the "turn on your location" banner only when we have no
        # browser location yet and the visitor didn't type an explicit one.
        "show_location_banner": (
            auto_loc.source != "cookie"
            and not (request.GET.get("zip") or request.GET.get("lat") or request.GET.get("lng"))
        ),
        # Map center for empty search results — the resolved reference point
        # (browser/IP location or searched-ZIP centroid) or None.
        "map_center_lat": origin_lat,
        "map_center_lng": origin_lng,
    }


@with_get_params
def page_map(request):
    from ..geo import resolve as resolve_loc
    q = (request.GET.get("q") or "").strip()
    # 5000 pharmacy rows + slugify per row is expensive; the unfiltered list is
    # user-independent so cache the rendered rows for an hour. Filtered views
    # (rare) rebuild each time.
    cache_key = "pp:map:rows:v1"
    if not q:
        cached = cache.get(cache_key)
        if cached is not None:
            auto_loc = resolve_loc(request)
            return {
                "map_pharmacies": cached,
                "marker_count": Row({"total": len(cached)}),
                "user_lat": auto_loc.lat,
                "user_lng": auto_loc.lng,
                "user_zip": auto_loc.zip,
            }
    qs = published_pharmacies().filter(
        is_digital=0,
        latitude__isnull=False, longitude__isnull=False,
    )
    if q:
        zip_q = re.sub(r"\D", "", q)[:5]
        q_filter = Q(name__icontains=q) | Q(city__icontains=q) | Q(address__icontains=q)
        if len(zip_q) >= 3:
            q_filter |= Q(zip__startswith=zip_q)
        if len(zip_q) == 5:
            # Resolve the ZIP to its location name; pharmacies have no ZIP.
            zip_city = _zip_city_state(zip_q)
            if zip_city:
                city_name, state_code = zip_city
                q_filter |= Q(city__iexact=city_name, state__iexact=state_code)
        qs = qs.filter(q_filter)
    qs = qs.order_by("-total_reviews", "pk")[:5000]
    rows = [_pharmacy_dict(p) for p in qs]
    if not q:
        cache.set(cache_key, rows, 3600)
    # Initial map center: user's location (auth zip → IP). Lets the map open
    # zoomed to their city instead of the whole continental US.
    auto_loc = resolve_loc(request)
    return {
        "map_pharmacies": rows,
        "marker_count": Row({"total": len(rows)}),
        "user_lat": auto_loc.lat,
        "user_lng": auto_loc.lng,
        "user_zip": auto_loc.zip,
    }


def page_online(request):
    q = (request.GET.get("q") or "").strip()
    # The online-pharmacy list is user-independent and large-ish (~1k rows);
    # cache the fully-rendered rows for an hour to keep slugify + ORM work
    # off the hot path.
    cache_key = "pp:online:list:v1"
    if not q:
        cached = cache.get(cache_key)
        if cached is not None:
            return {
                "online_pharms": cached,
                "pharmacies": cached,
                "total_count": Row({"total": len(cached)}),
            }
    qs = published_pharmacies().filter(is_digital=1)
    if q:
        qs = qs.filter(name__icontains=q)
    qs = qs.order_by("-avg_service_rating")
    rows = [_pharmacy_dict(p) for p in qs]
    if not q:
        cache.set(cache_key, rows, 3600)
    return {
        # Template iterates `{% for item in online_pharms %}` (legacy alias
        # name from the prod-port). Keep both to be safe.
        "online_pharms": rows,
        "pharmacies": rows,
        "total_count": Row({"total": len(rows)}),
    }


def page_pharmacy_detail(request, pharmacy_id=None):
    # Template renders {% for item in pharmacy %}{% for item2 in services %}...
    # — all five aliases come back as lists. Single-pharmacy pages return at
    # most one row in the `pharmacy` list.
    pid = _safe_int(pharmacy_id if pharmacy_id is not None
                    else request.GET.get("id"))
    if not pid:
        return {"pharmacy": [], "services": [], "coverage": [], "insurance": [], "hours": [], "breakdown": [], "reviews": []}
    p = Pharmacy.objects.filter(id=pid).first()
    if not p:
        return {"pharmacy": [], "services": [], "coverage": [], "insurance": [], "hours": [], "breakdown": [], "reviews": []}

    # Lazy Google Places enrichment for NPPES-sourced rows that don't yet
    # have lat/lng + hours. Runs in a background thread so this page render
    # isn't blocked. First pageview after sync triggers it; subsequent loads
    # see the populated data. ~$0.034 per pharmacy enriched, paid once per
    # row, no payment for pharmacies users never view.
    try:
        from ..enrich import needs_enrichment, enrich_pharmacy_async
        if needs_enrichment(p):
            enrich_pharmacy_async(p.id)
    except Exception:
        pass  # never block render on enrichment errors

    # Build the row from the same _pharmacy_dict shared with /list, /home,
    # etc. This way every page renders the same NPPES badges (verified,
    # established, services, chain). Add detail-page-only fields on top.
    pharmacy_row = _pharmacy_dict(p)
    pharmacy_row.update({
        "authorized_official_name": p.authorized_official_name or "",
        "authorized_official_title": p.authorized_official_title or "",
    })

    # Map embed + directions query: coords when we have them, else the full
    # address (Google geocodes it client-side, no API key needed). URL-quoted
    # so ampersands/commas survive into the iframe src and link.
    from urllib.parse import quote
    if p.latitude and p.longitude:
        map_query = f"{p.latitude},{p.longitude}"
    else:
        map_query = ", ".join(x for x in
                              (p.address, p.city, p.state, p.zip) if x)
    pharmacy_row["map_query"] = quote(map_query) if map_query else ""
    pharmacy_row["map_addr"] = map_query
    pharmacy_row["has_map"] = bool(map_query)

    services = list(p.services.values("service_type"))
    coverage = list(p.coverage_plans.order_by("plan_name").values(
        "id", "plan_name", "plan_type"))
    insurance = list(p.insurance_providers.order_by("provider_name").values(
        "id", "provider_name"))
    hours = list(p.hours.order_by("day_of_week").values(
        "day_of_week", "day_name", "open_time", "close_time", "is_closed"))
    approved = Review.objects.filter(
        pharmacy_id=pid, deleted_at__isnull=True, moderation_status="approved")
    breakdown = list(approved.values("service_rating")
                     .annotate(cnt=Count("id"))
                     .order_by("-service_rating"))
    reviews = list(approved.order_by("-created_at")[:20].values(
        "id", "user_name", "service_rating", "wait_time_rating",
        "stock_available", "comment", "is_caregiver",
        "response_text", "response_date", "created_at",
    ))

    return {
        "pharmacy":  [pharmacy_row],
        "services":  services,
        "coverage":  coverage,
        "insurance": insurance,
        "hours":     hours,
        "breakdown": breakdown,
        "reviews":   reviews,
    }


def page_compare(request):
    from ..geo import resolve as resolve_loc
    saved = []
    if request.user.is_authenticated:
        saved = [_pharmacy_dict(sc.pharmacy) for sc in
                 SavedComparison.objects.filter(user=request.user)
                 .select_related("pharmacy") if sc.pharmacy]
    zip_filter = (request.GET.get("zip") or "").strip()

    # Auto-populate zip preview when no explicit zip given. Same precedence
    # chain as /list: explicit ?zip= wins, then authed user zip, then IP.
    auto_loc = None
    if not zip_filter:
        auto_loc = resolve_loc(request)
        if auto_loc.zip:
            zip_filter = auto_loc.zip

    # Top-5 nearby for non-authed preview; authed users use their saved set.
    zip_results = []
    if zip_filter and not request.user.is_authenticated:
        zip_results = [_pharmacy_dict(p) for p in
                       published_pharmacies().filter(
                           is_digital=0,
                           zip__startswith=zip_filter[:3])
                       .order_by("-avg_service_rating")[:5]]

    return {
        "saved": saved,
        "zip_results": zip_results,
        "winners_saved": _compare_winners(saved),
        "winners_zip":   _compare_winners(zip_results),
        "zip": zip_filter,
        "auto_loc_zip":    auto_loc.zip    if auto_loc else "",
        "auto_loc_city":   auto_loc.city   if auto_loc else "",
        "auto_loc_state":  auto_loc.state  if auto_loc else "",
        "auto_loc_source": auto_loc.source if auto_loc else "",
    }


def page_claim(request):
    pid = _safe_int(request.GET.get("pharmacy_id") or request.GET.get("id"))
    p = Pharmacy.objects.filter(id=pid).first() if pid else None
    existing = []
    if request.user.is_authenticated:
        existing = list(PharmacyClaim.objects.filter(user=request.user)
                        .order_by("-created_at")[:1].values(
            "id", "status", "rejection_reason", "pharmacy_name", "created_at",
        ))
    return {
        "pharmacy": Row(_pharmacy_dict(p)) if p else None,
        "pharmacy_id": p.id if p else "",
        "pharmacy_name": p.name if p else request.GET.get("pharmacy_name", ""),
        "existing_claim": existing,
        "error": request.GET.get("error"),
    }


def page_widget(request):
    pid = _safe_int(request.GET.get("pharmacy_id") or request.GET.get("id"))
    p = Pharmacy.objects.filter(id=pid).first() if pid else None
    p_rows = []
    if p:
        p_rows = [{
            "id": p.id, "name": p.name,
            "avg_service_rating": p.avg_service_rating,
            "total_reviews": p.total_reviews,
        }]
    return {
        "pharmacy": Row(_pharmacy_dict(p)) if p else None,
        "p": p_rows,  # template iterates `{% for item in p %}`
    }
