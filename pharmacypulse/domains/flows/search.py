"""/api/flow/search autocomplete endpoint."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from django.conf import settings
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.cache import cache
from django.db import transaction
from django.db.models import F, Sum
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse, HttpResponseBadRequest
from django.shortcuts import redirect
from django.utils import timezone as djtz
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from ...models import (
    ActivityLog, DataRequest, DrugShortage, NewsletterSubscriber, Notification,
    Pharmacy, PharmacyClaim, PharmacyHours, PharmacyOrg, PharmacyTeamMember,
    ResponseCount, Review, ReviewResponse, SavedComparison, User, UserConsent,
)

from .common import admin_required, _activity
from ...domains.common import _zip_city_state

_AUTOCOMPLETE_URL = "https://maps.googleapis.com/maps/api/place/autocomplete/json"
_DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"

# A query only hits Google Places Autocomplete when it plausibly describes a
# street address (a house number followed by a word, or a street-suffix word).
# Plain cities, states, and ZIPs stay on the free DB path — this keeps per-
# keystroke autocomplete billing off non-address typing.
_STREET_SUFFIXES = {
    "st", "street", "ave", "avenue", "rd", "road", "blvd", "boulevard",
    "dr", "drive", "ln", "lane", "way", "ct", "court", "pl", "place",
    "pike", "hwy", "highway", "route", "pkwy", "parkway", "cir", "circle",
}
# City names that contain a street-suffix-looking word ("St. Louis", "Mt.
# Vernon", "Ft. Worth", "Two Rivers", "New Hope") — these should stay on the
# free DB city path, not fire a Google address lookup.
_GEO_NAME_TOKENS = {
    "st", "mt", "ft", "saint", "san", "santa", "north", "south", "east",
    "west", "new", "old", "upper", "lower", "two", "three", "red",
}

_HOUSE_NUMBER_RE = re.compile(r"\d{1,6}(?:st|nd|rd|th)?\s+\w", re.IGNORECASE)


def _looks_like_address(q: str) -> bool:
    """True when a query reads like a street address rather than a city/state/
    ZIP. Drives whether we spend a Google Autocomplete call on it. Geo-prefix
    tokens ("St.", "Mt.", "New", ...) only exempt when they lead the query —
    "Market St" is an address, "St. Louis" is a city."""
    if _HOUSE_NUMBER_RE.search(q):
        return True
    tokens = re.split(r"[\s,]+", q)
    for i, tok in enumerate(tokens):
        t = re.sub(r"[^a-z]", "", tok.lower())
        if not t:
            continue
        if t in _STREET_SUFFIXES and (i > 0 or t not in _GEO_NAME_TOKENS):
            return True
    return False


def _google_place_suggestions(q: str) -> list:
    """Server-side Google Places Autocomplete for street addresses. The API key
    stays out of the browser. Cached by query for a day so retyping the same
    address (or a crawler hammering it) doesn't re-bill Google."""
    key = settings.GOOGLE_PLACES_KEY
    if not key:
        return []
    cache_key = "pp:place-suggest:" + hashlib.sha256(q.lower().encode()).hexdigest()
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    try:
        r = requests.get(
            _AUTOCOMPLETE_URL,
            params={
                "input": q,
                "types": "address",
                "components": "country:us",
                "region": "us",
                "key": key,
            },
            timeout=4,
        )
        data = r.json()
    except (requests.RequestException, ValueError):
        return []
    out = []
    for p in (data.get("predictions") or [])[:4]:
        place_id = p.get("place_id")
        desc = p.get("description")
        if place_id and desc:
            out.append({"label": desc, "type": "address", "place_id": place_id})
    cache.set(cache_key, out, timeout=86400)
    return out


def place_details(request):
    """Resolve a picked Google Place (from location autocomplete) to
    coordinates. Cached per place_id for 30 days so a repeated pick never
    re-bills Place Details."""
    place_id = (request.GET.get("place_id") or "").strip()
    if not place_id:
        return JsonResponse({"error": "place_id required"}, status=400)
    key = settings.GOOGLE_PLACES_KEY
    if not key:
        return JsonResponse({"error": "GOOGLE_PLACES_KEY not configured"}, status=503)
    cache_key = "pp:place-detail:" + hashlib.sha256(place_id.encode()).hexdigest()
    cached = cache.get(cache_key)
    if cached is not None:
        return JsonResponse(cached)
    try:
        r = requests.get(
            _DETAILS_URL,
            params={
                "place_id": place_id,
                "fields": "geometry,formatted_address",
                "key": key,
            },
            timeout=5,
        )
        data = r.json()
    except (requests.RequestException, ValueError):
        return JsonResponse({"error": "place lookup failed"}, status=502)
    result = data.get("result") or {}
    loc = (result.get("geometry") or {}).get("location") or {}
    lat, lng = loc.get("lat"), loc.get("lng")
    if lat is None or lng is None:
        return JsonResponse({"error": "place has no coordinates"}, status=404)
    payload = {
        "place_id": place_id,
        "lat": lat,
        "lng": lng,
        "formatted_address": result.get("formatted_address", ""),
    }
    cache.set(cache_key, payload, timeout=30 * 86400)
    return JsonResponse(payload)


def search_pharmacies(request):
    from django.db.models import Q as _Q, OuterRef, Subquery, F
    from django.db.models.functions import Coalesce
    from ...domains.common import published_pharmacies, _distance_mi_expr, _zip_city_state
    from ...geo import resolve as resolve_loc
    from ...models import ZipCentroid
    from ...domains.pharmacies import _resolve_origin

    q = (request.GET.get("q") or "").strip()
    qs = published_pharmacies()

    # Resolve location for distance-aware ordering
    loc = resolve_loc(request)
    origin_lat, origin_lng = _resolve_origin(loc, loc.zip)

    if q:
        # Review picker + search autocomplete both call with a query string.
        zip_q = re.sub(r"\D", "", q)[:5]
        # Name matching is token-ANDed so word separators don't matter —
        # "rite aid" matches "RITE-AID PHARMACY" (NPPES hyphenates many
        # chains). City/address stay as substring matches.
        q_filter = _Q(city__icontains=q) | _Q(address__icontains=q)
        name_tokens = re.findall(r"[a-zA-Z0-9]+", q)
        name_q = _Q()
        for token in name_tokens:
            name_q &= _Q(name__icontains=token)
        if name_tokens:
            q_filter |= name_q
        if len(zip_q) >= 3:
            q_filter |= _Q(zip__startswith=zip_q)
        if len(zip_q) == 5:
            # Pharmacies have no ZIP → resolve the ZIP to its location name
            # and match pharmacies within that location.
            zip_city = _zip_city_state(zip_q)
            if zip_city:
                city_name, state_code = zip_city
                q_filter |= _Q(city__iexact=city_name, state__iexact=state_code)
            cen = ZipCentroid.objects.filter(zip=zip_q).first()
            if cen and cen.latitude and cen.longitude:
                lat, lng = cen.latitude, cen.longitude
                q_filter |= (_Q(latitude__gte=lat - 0.15, latitude__lte=lat + 0.15,
                               longitude__gte=lng - 0.2, longitude__lte=lng + 0.2))
        qs = qs.filter(q_filter)
    elif request.GET.get("popular") == "1":
        # Review picker initial load: show nearest published pharmacies
        pass
    else:
        return JsonResponse({"results": []})

    if origin_lat is not None and origin_lng is not None:
        cen_lat = Subquery(
            ZipCentroid.objects.filter(zip=OuterRef("zip")).values("latitude")[:1])
        cen_lng = Subquery(
            ZipCentroid.objects.filter(zip=OuterRef("zip")).values("longitude")[:1])
        qs = qs.annotate(
            eff_lat=Coalesce(F("latitude"), cen_lat),
            eff_lng=Coalesce(F("longitude"), cen_lng),
        ).annotate(distance_mi=_distance_mi_expr(origin_lat, origin_lng, "eff_lat", "eff_lng"))
        qs = qs.order_by("distance_mi", "-total_reviews", "name")[:8]
    else:
        qs = qs.order_by("-total_reviews", "name")[:8]

    from ...text_utils import pharmacy_slug
    results = [{
        "id": p.id, "name": p.name, "address": p.address, "city": p.city,
        "state": p.state, "zip": p.zip, "phone": p.phone,
        "avg_service_rating": p.avg_service_rating,
        "total_reviews": p.total_reviews, "is_digital": p.is_digital,
        "distance_mi": round(getattr(p, "distance_mi"), 1) if getattr(p, "distance_mi", None) is not None else None,
        "slug": pharmacy_slug(p.name),
    } for p in qs]
    return JsonResponse({"results": results})


def suggest_locations(request):
    """Location autocomplete for the hero search. Suggests ZIPs (with their
    city/state), cities that actually have pharmacies, and US states, so typing
    an address, location, state, or city surfaces a pickable suggestion. Each
    suggestion carries the server fields the /list search understands
    (zip / city / state)."""
    from django.db.models import Q as _Q
    from ...domains.common import _STATE_NAMES, _state_code, published_pharmacies
    from ...models import ZipCentroid

    q = (request.GET.get("q") or "").strip()
    if len(q) < 2:
        return JsonResponse({"results": []})

    results = []
    seen = set()
    digits = re.sub(r"\D", "", q)
    trimmed = q.strip()

    # A bare 2-letter code ("PA" / "pa") is unambiguously a state — offer it
    # directly instead of matching unrelated cities that contain the letters.
    code = _state_code(q)
    if code and not digits and len(trimmed) <= 2:
        return JsonResponse({"results": [{
            "label": _STATE_NAMES.get(code, code), "type": "state",
            "state": code, "state_name": _STATE_NAMES.get(code, code),
        }]})

    # 0. Street-address input → Google Places Autocomplete (proxied, cached).
    #    Addresses are the most specific matches so they surface first; they
    #    carry a place_id the JS resolves to coordinates on pick. House numbers
    #    must NOT be treated as ZIP prefixes below, so an address query skips
    #    the digit-based ZIP branch.
    is_addr = _looks_like_address(q)
    if is_addr:
        addr = _google_place_suggestions(q)
        if addr:
            results = addr + results

    # 1. ZIP matches from ZipCentroid and published pharmacies
    if digits and not is_addr:
        target_prefix = digits[:5]
        from django.db.models import Case, When, Value, IntegerField
        zip_qs = ZipCentroid.objects.filter(zip__startswith=target_prefix).annotate(
            is_exact=Case(
                When(zip=target_prefix, then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            )
        ).order_by("is_exact", "zip")
        for zc in zip_qs[:10]:
            label = f"{zc.zip} — {zc.city}, {zc.state}" if zc.city else zc.zip
            if zc.zip in seen:
                continue
            seen.add(zc.zip)
            results.append({
                "label": label, "type": "zip", "zip": zc.zip,
                "city": zc.city, "state": zc.state,
                "state_name": _STATE_NAMES.get(zc.state, zc.state),
            })
            if len(results) >= 8:
                return JsonResponse({"results": results})

        # Supplement with any ZIPs stored on published pharmacies
        pharm_zips = (published_pharmacies()
                      .filter(zip__startswith=target_prefix)
                      .exclude(zip="")
                      .values("zip", "city", "state")
                      .distinct()[:8])
        for pz in pharm_zips:
            pz_clean = (pz["zip"] or "").strip()[:5]
            if not pz_clean or pz_clean in seen:
                continue
            seen.add(pz_clean)
            label = f"{pz_clean} — {pz['city']}, {pz['state']}" if pz.get("city") else pz_clean
            results.append({
                "label": label, "type": "zip", "zip": pz_clean,
                "city": pz.get("city", ""), "state": pz.get("state", ""),
                "state_name": _STATE_NAMES.get(pz.get("state"), pz.get("state")),
            })
            if len(results) >= 8:
                return JsonResponse({"results": results})
    else:
        zip_qs = ZipCentroid.objects.filter(city__icontains=q)
        for zc in zip_qs.order_by("zip")[:8]:
            label = f"{zc.zip} — {zc.city}, {zc.state}" if zc.city else zc.zip
            if zc.zip in seen:
                continue
            seen.add(zc.zip)
            results.append({
                "label": label, "type": "zip", "zip": zc.zip,
                "city": zc.city, "state": zc.state,
                "state_name": _STATE_NAMES.get(zc.state, zc.state),
            })
            if len(results) >= 8:
                return JsonResponse({"results": results})

    # 2. City matches from the published pharmacy set — real cities that have
    #    pharmacies, deduped by city+state.
    city_qs = (published_pharmacies()
               .filter(city__icontains=q)
               .exclude(city="")
               .values("city", "state")
               .distinct()[:25])
    for row in city_qs:
        key = (f"{row['city'].strip().lower()}|{row['state'] or ''}")
        if key in seen:
            continue
        seen.add(key)
        results.append({
            "label": f"{row['city']}, {row['state']}", "type": "city",
            "city": row["city"], "state": row["state"] or "",
            "state_name": _STATE_NAMES.get(row["state"] or "", row["state"] or ""),
        })
        if len(results) >= 8:
            break

    # 3. A US state name or 2-letter code typed on its own → offer the state.
    code = _state_code(q)
    if code and code not in seen:
        seen.add(code)
        results.append({
            "label": _STATE_NAMES.get(code, code), "type": "state",
            "state": code, "state_name": _STATE_NAMES.get(code, code),
        })

    return JsonResponse({"results": results})
