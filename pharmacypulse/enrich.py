"""Google Places enrichment for NPPES-sourced pharmacy rows.

NPPES gives us name + address + phone but not lat/lng or operating hours.
This module looks up a Pharmacy on Google Places by name + address (Find
Place text search), then pulls Place Details to populate the missing
fields. Idempotent — once a row has place_id + lat/lng, we don't re-call.

Lazy use: page_pharmacy_detail kicks off a thread to enrich the viewed
pharmacy if it's missing data. Bulk use: a future management command
can iterate enriched=False rows.

Cost: Find Place ($0.017) + Details ($0.017) = ~$0.034 per pharmacy
enriched. Cached forever — a row only enriches once.
"""
from __future__ import annotations

import logging
import threading
from datetime import timedelta
from typing import Optional

import requests
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone as djtz

from .models import Pharmacy, PharmacyHours

log = logging.getLogger(__name__)

_FIND_PLACE_URL = "https://maps.googleapis.com/maps/api/place/findplacefromtext/json"
_DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"

# Re-attempt cooldown for rows that have already been checked. 6 months
# matches "things change rarely" — pharmacy moves/renames/hour-changes
# happen but not often. After this window we'll re-check Google in case
# they've added a listing they didn't have before.
_RECHECK_COOLDOWN = timedelta(days=180)


_DAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]


def _format_time(s: str) -> str:
    """Convert Google's 'HHMM' time string to '11:30 AM' style. Mirrors the
    helper in flows.py — kept inline so this module has no flows.py dep."""
    s = (s or "").strip()
    if not s or len(s) < 4:
        return s or ""
    try:
        h, m = int(s[:2]), s[2:4]
    except ValueError:
        return s
    if h == 0:
        return f"12:{m} AM"
    if h < 12:
        return f"{h}:{m} AM"
    if h == 12:
        return f"12:{m} PM"
    return f"{h - 12}:{m} PM"


def needs_enrichment(p: Pharmacy) -> bool:
    """A row is enrichable if all of:
      (a) NPPES-sourced (has a real 10-digit NPI)
      (b) not a mail-order pharmacy (no physical location to map)
      (c) missing data Google supplies (lat/lng or hours)
      (d) NOT inside the 6-month recheck cooldown — even if data is still
          missing, we won't re-poll Google more often than every 6 months.
          Stops a crawler / retry loop from running up the API bill on
          rows Google can't find.
    """
    if not p.npi_number or not p.npi_number.isdigit() or len(p.npi_number) != 10:
        return False
    if p.is_digital:
        return False
    has_geo = bool(p.latitude and p.longitude)
    has_hours = p.hours.exists() if p.id else False
    if has_geo and has_hours:
        return False  # already enriched, no work to do
    # Cooldown check — if we've attempted enrichment recently, don't re-poll
    # even if the row is still missing data (Google Places didn't have a
    # match the first time and probably still doesn't).
    if p.enrichment_checked_at:
        age = djtz.now() - p.enrichment_checked_at
        if age < _RECHECK_COOLDOWN:
            return False
    return True


def enrich_pharmacy(pharmacy_id: int) -> None:
    """Synchronously enrich one Pharmacy row. Designed to be called from a
    background thread; never raises into the caller."""
    # Inter-process lock via cache to deduplicate concurrent attempts. If 10
    # users hit the same pharmacy detail page at once, only ONE thread should
    # actually call Google. Lock TTL of 60s covers a typical Google round-trip
    # without leaving a stale lock around.
    lock_key = f"enrich_lock:{pharmacy_id}"
    if cache.get(lock_key):
        return  # another thread is already enriching this row
    cache.set(lock_key, 1, timeout=60)
    try:
        p = Pharmacy.objects.filter(id=pharmacy_id).first()
        if p is None or not needs_enrichment(p):
            return
        key = settings.GOOGLE_PLACES_KEY
        if not key:
            return

        # Stamp the attempt timestamp regardless of outcome — the early-return
        # paths below all imply Google didn't help this pharmacy, and we don't
        # want to retry them on the next pageview. This is what makes the
        # cooldown effective.
        now = djtz.now()
        Pharmacy.objects.filter(id=pharmacy_id).update(enrichment_checked_at=now)

        # Find Place text search — input is the most distinguishing query
        # we have: "<name>, <street>, <city> <state> <zip>".
        query_parts = [p.name, p.address, p.city, p.state, p.zip]
        query = ", ".join(part for part in query_parts if part).strip(", ")
        if not query:
            return
        try:
            r = requests.get(
                _FIND_PLACE_URL,
                params={
                    "input": query,
                    "inputtype": "textquery",
                    "fields": "place_id,geometry,name",
                    "key": key,
                },
                timeout=8,
            )
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            log.warning("Find Place failed for pharmacy_id=%s: %s", pharmacy_id, e)
            return
        candidates = data.get("candidates") or []
        if not candidates:
            log.info("No Google Places match for pharmacy_id=%s (%s)", pharmacy_id, p.name)
            return
        cand = candidates[0]
        place_id = cand.get("place_id")
        if not place_id:
            return
        geo = (cand.get("geometry") or {}).get("location") or {}
        lat = geo.get("lat")
        lng = geo.get("lng")

        # Pull full details for hours + website + business_status.
        # business_status: OPERATIONAL | CLOSED_TEMPORARILY | CLOSED_PERMANENTLY.
        # NPPES doesn't track closures, so this is the cheapest way to catch
        # Rite Aid bankruptcies, individual permanent shutdowns, etc.
        try:
            r = requests.get(
                _DETAILS_URL,
                params={
                    "place_id": place_id,
                    "fields": "opening_hours,website,formatted_phone_number,business_status",
                    "key": key,
                },
                timeout=8,
            )
            detail = (r.json() or {}).get("result") or {}
        except (requests.RequestException, ValueError) as e:
            log.warning("Place Details failed for pharmacy_id=%s: %s", pharmacy_id, e)
            detail = {}

        # If Google reports the location permanently closed, soft-delete the
        # pharmacy by setting status='closed'. The list query already filters
        # status='active' so it disappears from results without losing review
        # history.
        if detail.get("business_status") == "CLOSED_PERMANENTLY":
            Pharmacy.objects.filter(id=pharmacy_id).update(
                status="closed", place_id=place_id,
            )
            log.info("Marked pharmacy_id=%s as closed (Google: CLOSED_PERMANENTLY)", pharmacy_id)
            return

        updates = {"place_id": place_id}
        if lat and lng:
            updates["latitude"] = lat
            updates["longitude"] = lng
        if not p.website and detail.get("website"):
            updates["website"] = detail["website"]
        if not p.phone and detail.get("formatted_phone_number"):
            updates["phone"] = detail["formatted_phone_number"]
        Pharmacy.objects.filter(id=pharmacy_id).update(**updates)

        # Hours — replace any existing rows for this pharmacy.
        for period in (detail.get("opening_hours") or {}).get("periods", []) or []:
            day = period.get("open", {}).get("day")
            if day is None:
                continue
            PharmacyHours.objects.update_or_create(
                pharmacy_id=pharmacy_id, day_of_week=day,
                defaults={
                    "day_name": _DAY_NAMES[day] if 0 <= day <= 6 else "Unknown",
                    "open_time": _format_time(period.get("open", {}).get("time", "")),
                    "close_time": _format_time(period.get("close", {}).get("time", "")),
                },
            )
    except Exception as e:  # noqa: BLE001 — never let enrichment crash a render
        log.warning("enrich_pharmacy(%s) crashed: %s", pharmacy_id, e)


def enrich_pharmacy_async(pharmacy_id: int) -> None:
    """Fire-and-forget enrichment. Spawns a daemon thread so the page render
    doesn't block on Google's API. The thread dies with the dyno; no cleanup
    needed since enrichment is idempotent."""
    t = threading.Thread(
        target=enrich_pharmacy, args=(pharmacy_id,), daemon=True,
    )
    t.start()
