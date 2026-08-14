"""Google Places pharmacy ingestion + lazy detail enrichment."""
from __future__ import annotations

import json
import re
import secrets
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from django.conf import settings
from django.contrib.auth.decorators import login_required, user_passes_test
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


_DAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]


def _format_time(t: str) -> str:
    """"0900" → "9:00 AM" formatting to match Benmore SQL's conversion."""
    if not t or len(t) < 4:
        return t or ""
    try:
        h = int(t[:2]); m = t[2:4]
    except ValueError:
        return t
    if h == 0:
        return f"12:{m} AM"
    if h < 12:
        return f"{h}:{m} AM"
    if h == 12:
        return f"12:{m} PM"
    return f"{h - 12}:{m} PM"


def _extract_city_state(address_components: list) -> tuple[str, str]:
    """Pull city + state abbreviation out of a Google address_components list."""
    city, state = "", ""
    for comp in address_components or []:
        types = comp.get("types", [])
        if "locality" in types:
            city = comp.get("long_name", "")
        elif "administrative_area_level_1" in types:
            state = comp.get("short_name", "")
    return city, state


_VET_PATTERN = re.compile(
    r"(?i)\bveterinary\b|\bvet clinic\b|\bvet hospital\b|"
    r"animal hospital|animal clinic|"
    r"\bpet hospital\b|\bpet clinic\b|pet healthcare"
)


_INDIVIDUAL_3_WORDS = re.compile(r"^[A-Z][a-z'\-]+ [A-Z][a-z'\-]+ [A-Z]$")


_BIZ_WORDS = {
    "pharmacy", "pharmacies", "drug", "drugs", "rx", "apothecary", "apothecaries",
    "health", "healthcare", "wellness", "care", "clinic", "clinics",
    "medical", "medicine", "medicines", "compound", "compounding",
    "center", "centre", "centers", "centres", "hospital", "hospitals",
    "hospice", "hospices", "system", "systems", "regional", "community",
    "family", "neighborhood", "associates", "associate", "associated", "group",
    "discount", "value", "valu", "express", "quick", "fast", "good",
    "online", "digital", "mail", "delivery", "specialty", "specialist",
    "nursing", "supply", "supplies", "lab", "labs", "laboratory", "laboratories",
    "therapeutics", "therapy", "therapies", "biotech", "pharma",
    "pharmaceutical", "pharmaceuticals", "diagnostic", "diagnostics",
    "isotopes", "isotope",
    "inc", "llc", "corp", "co", "ltd", "company", "limited",
    "of", "the", "and", "&", "+", "for", "by", "at", "in",
    "store", "stores", "mart", "shop", "shoppe",
    "save", "savon", "savings", "rite", "aid", "walgreens", "walmart",
    "cvs", "kroger", "publix", "target", "costco", "sams",
    "wegmans", "albertsons", "safeway", "vons", "ralphs", "stop", "shop",
    "us", "usa", "national", "state", "city", "downtown", "uptown",
    "north", "south", "east", "west", "central", "main", "first", "second",
    "specialty", "professional", "advanced", "preferred", "select",
    "complete", "premium", "elite", "trust", "trusted", "quality",
    "best", "better", "premier", "primary", "general",
    "campus", "university", "college", "school", "academy", "institute",
    "veterans", "military", "military", "tribal", "indian",
    "rural", "metro", "urban", "valley", "ridge", "hill", "park",
    "rapidcare", "rapid", "urgent", "immediate", "emergency",
    "mail-out", "mailorder", "mail-order",
    "discount", "savings", "advantage",
}


def _looks_like_individual(name: str) -> bool:
    """True if `name` looks like a 2-3 word personal name with no business indicator.
    Catches 'Shiyun Kim', 'Zambrano Marco', 'Smith James D' etc. Stays away from
    real businesses by requiring NO recognized pharmacy/business word in the name.

    Trade-off: a few obscure 2-word business names (e.g. 'Triad Isotopes' caught
    by the 'isotopes' biz word, but 'Acme Compounders' would NOT — 'compounders'
    isn't in the list) may slip through either way. We err on the side of
    excluding individuals over keeping every edge-case business — it's worse to
    show 'Zambrano Marco' to a patient looking for their pharmacy."""
    parts = name.split()
    if not 2 <= len(parts) <= 4:
        return False
    # If ANY word matches a business indicator, treat as a business, not individual.
    for w in parts:
        cleaned = w.lower().strip(".,'\"()&").rstrip("'s")
        if cleaned in _BIZ_WORDS:
            return False
    # All words must look name-like: start with uppercase letter, mostly alpha,
    # not all-caps acronyms (CVS, NPI, etc).
    for w in parts:
        if not w:
            return False
        if not w[0].isalpha() or not w[0].isupper():
            return False
        if w.isupper() and len(w) >= 2:
            # All-caps word like 'CVS', 'NPI', 'USA' — likely a brand/acronym.
            return False
    return True


def _is_excluded_pharmacy(name: str, types: list | None = None) -> bool:
    """Filter out names that aren't real consumer-facing retail pharmacies."""
    n = (name or "").strip()
    if not n:
        return True
    # Place-type filter (vets reliably tagged by Google).
    if types and "veterinary_care" in types:
        return True
    if _VET_PATTERN.search(n):
        return True
    # Personal-credentials suffix.
    n_lower = n.lower()
    if (n_lower.endswith(", rph") or n_lower.endswith(", pharmd")
            or n_lower.endswith(", rph.") or n_lower.endswith(", pharmd.")
            or ", rph " in n_lower or ", pharmd " in n_lower):
        return True
    # 3-word "Last First M" pattern (single capital letter at end).
    if _INDIVIDUAL_3_WORDS.match(n):
        return True
    # General individual-name heuristic for 2-4 word names with no business indicator.
    if _looks_like_individual(n):
        return True
    return False


def _country_code(address_components: list) -> str:
    """Return the ISO country short_name (e.g., 'US', 'MX', 'CA') from address_components."""
    for comp in address_components or []:
        if "country" in comp.get("types", []):
            return comp.get("short_name", "")
    return ""


def ingest_zip(key: str, zip_code: str, radius: str = "5000", state: str = "") -> dict:
    """Core sync logic — fetch all pharmacies near a ZIP via Google Places and
    upsert into the DB.  Returns {"inserted": int, "total": int, "error": str|None}.
    Reused by both the admin flow endpoint and the sync_pharmacies management command.

    `state`: optional 2-letter state code, used to disambiguate the geocode call.
    Bare ZIPs cause Google to return international postal codes ('02101' → Vilnius LT)
    or, with components=country:US bias, the geographic centroid of the US. Passing
    "<zip> <state>, USA" hard-anchors the lookup."""
    import time

    # Geocode ZIP → lat/lng + fallback city/state.
    address = f"{zip_code} {state}, USA" if state else f"{zip_code}, USA"
    try:
        geo = requests.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params={"address": address, "key": key},
            timeout=15,
        ).json()
        geo_result = geo["results"][0]
        loc = geo_result["geometry"]["location"]
        fallback_city, fallback_state = _extract_city_state(
            geo_result.get("address_components", [])
        )
    except Exception:
        return {"inserted": 0, "total": 0, "error": f"Could not geocode ZIP {zip_code}"}

    # Paginate through nearbysearch — Google returns up to 20 per page, 3 pages max
    all_places: list = []
    params: dict = {
        "location": f"{loc['lat']},{loc['lng']}",
        "radius": radius,
        "type": "pharmacy",
        "key": key,
    }
    for _ in range(3):
        try:
            resp = requests.get(
                "https://maps.googleapis.com/maps/api/place/nearbysearch/json",
                params=params, timeout=20,
            ).json()
        except requests.RequestException:
            break
        all_places.extend(resp.get("results") or [])
        token = resp.get("next_page_token")
        if not token:
            break
        time.sleep(2)  # Google requires a short delay before next_page_token is valid
        params = {"pagetoken": token, "key": key}

    inserted = 0
    for place in all_places:
        place_id = place.get("place_id")
        if not place_id:
            continue
        # Cheap pre-filter before paying for Place Details: drop vet hospitals
        # and individual practitioners by name + types. Saves an API call per
        # exclusion.
        if _is_excluded_pharmacy(place.get("name", ""), place.get("types") or []):
            continue

        # Details: address_components, phone, hours, website
        try:
            detail = requests.get(
                "https://maps.googleapis.com/maps/api/place/details/json",
                params={
                    "place_id": place_id,
                    "fields": "formatted_phone_number,opening_hours,website,address_components,formatted_address",
                    "key": key,
                },
                timeout=10,
            ).json().get("result", {})
        except requests.RequestException:
            detail = {}

        components = detail.get("address_components", [])
        # Border-city radius searches (El Paso/Juarez, Detroit/Windsor, Buffalo/Niagara)
        # pull pharmacies from across the border. Drop anything that isn't actually in the US.
        if _country_code(components) not in ("", "US"):
            continue
        city, state = _extract_city_state(components)
        if not city:
            city = fallback_city
        if not state:
            state = fallback_state

        pharm, created = Pharmacy.objects.get_or_create(
            npi_number=place_id,
            defaults={
                "name": place.get("name", "")[:255],
                "address": (detail.get("formatted_address") or place.get("vicinity", ""))[:255],
                "zip": zip_code,
                "city": city,
                "state": state,
                "latitude": place["geometry"]["location"]["lat"],
                "longitude": place["geometry"]["location"]["lng"],
                "status": "active",
                "phone": detail.get("formatted_phone_number", ""),
                "website": detail.get("website", ""),
            },
        )
        if created:
            inserted += 1
        else:
            # Backfill city/state on existing rows that were ingested with blank values
            updates = {}
            if not pharm.city and city:
                updates["city"] = city
            if not pharm.state and state:
                updates["state"] = state
            if not pharm.phone and detail.get("formatted_phone_number"):
                updates["phone"] = detail["formatted_phone_number"]
            if not pharm.website and detail.get("website"):
                updates["website"] = detail["website"]
            if updates:
                Pharmacy.objects.filter(pk=pharm.pk).update(**updates)

        for period in (detail.get("opening_hours") or {}).get("periods", []) or []:
            day = period.get("open", {}).get("day")
            if day is None:
                continue
            PharmacyHours.objects.get_or_create(
                pharmacy=pharm, day_of_week=day,
                defaults={
                    "day_name": _DAY_NAMES[day] if 0 <= day <= 6 else "Unknown",
                    "open_time": _format_time(period.get("open", {}).get("time", "")),
                    "close_time": _format_time(period.get("close", {}).get("time", "")),
                },
            )

    return {"inserted": inserted, "total": len(all_places), "error": None}


@admin_required
def ingest_pharmacies_by_zip(request):
    key = settings.GOOGLE_PLACES_KEY
    if not key:
        return redirect("/admin-pharmacies?error=GOOGLE_PLACES_KEY+is+not+configured")
    zip_code = (request.POST.get("zip_code") or "").strip()
    radius = request.POST.get("radius") or "5000"
    if not zip_code:
        return redirect("/admin-pharmacies?error=ZIP+required")
    result = ingest_zip(key, zip_code, radius)
    if result["error"]:
        return redirect(f"/admin-pharmacies?error={urllib.parse.quote(result['error'])}")
    _activity(request, "pharmacy_ingest",
              f"ZIP {zip_code}: {result['total']} found, {result['inserted']} new")
    return redirect(
        f"/admin-pharmacies?success={result['total']}+results+for+{zip_code}+({result['inserted']}+new)"
    )


def fetch_pharmacy_details(request, pharmacy_id: int):
    key = settings.GOOGLE_PLACES_KEY
    if not key:
        return redirect(f"/pharmacy?id={pharmacy_id}&error=GOOGLE_PLACES_KEY+not+configured")
    p = Pharmacy.objects.filter(id=pharmacy_id).first()
    if not p or not p.npi_number:
        return redirect(f"/pharmacy?id={pharmacy_id}&error=Pharmacy+missing+place_id")
    try:
        detail = requests.get(
            "https://maps.googleapis.com/maps/api/place/details/json",
            params={"place_id": p.npi_number,
                     "fields": "formatted_phone_number,opening_hours,website,formatted_address,address_components",
                     "key": key},
            timeout=10,
        ).json().get("result", {})
    except requests.RequestException:
        return redirect(f"/pharmacy?id={pharmacy_id}&error=Google+Places+request+failed")
    city, state = _extract_city_state(detail.get("address_components", []))
    Pharmacy.objects.filter(id=pharmacy_id).update(
        phone=detail.get("formatted_phone_number", "") or "",
        website=detail.get("website", "") or "",
        address=detail.get("formatted_address", "") or p.address,
        city=city or p.city,
        state=state or p.state,
    )
    PharmacyHours.objects.filter(pharmacy_id=pharmacy_id).delete()
    for period in (detail.get("opening_hours") or {}).get("periods", []) or []:
        day = period.get("open", {}).get("day")
        if day is None:
            continue
        PharmacyHours.objects.create(
            pharmacy_id=pharmacy_id, day_of_week=day,
            day_name=_DAY_NAMES[day] if 0 <= day <= 6 else "Unknown",
            open_time=period.get("open", {}).get("time", ""),
            close_time=period.get("close", {}).get("time", ""),
        )
    _activity(request, "pharmacy_detail_fetch", f"Fetched details for {p.name}")
    return redirect(f"/pharmacy?id={pharmacy_id}")
