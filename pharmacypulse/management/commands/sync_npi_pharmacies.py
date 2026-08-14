"""Sync US pharmacies from the NPPES NPI Registry.

NPPES (CMS) is the authoritative source for every NPI-registered pharmacy
in the US. Querying with enumeration_type=NPI-2 (organizations only) and
filtering by pharmacy taxonomy codes guarantees no individual practitioners
or vet clinics — they have different taxonomy codes (1835X..., 174M..., etc.).

API: https://npiregistry.cms.hhs.gov/api/?version=2.1
Free, no auth. Returns up to 200 results per request, skip up to 1000 →
max 1200 records per filter combo.

Strategy: paginate per ZIP-2 prefix (00..99) per pharmacy taxonomy. Postal
codes don't cross state lines (mostly), and each ZIP-2 area has fewer
pharmacies than a full state — keeps us under the 1200-per-query cap even
in dense regions.

Pharmacy taxonomies pulled (NUCC code → our `is_digital` flag):
  3336C0003X  Community/Retail Pharmacy   → is_digital=0
  3336M0002X  Mail Order Pharmacy         → is_digital=1
  3336S0011X  Specialty Pharmacy          → is_digital=0
  3336C0004X  Compounding Pharmacy        → is_digital=0

Usage:
  python manage.py sync_npi_pharmacies                    # full US sync
  python manage.py sync_npi_pharmacies --zip-prefix 19    # one prefix
  python manage.py sync_npi_pharmacies --state PA         # one state
  python manage.py sync_npi_pharmacies --limit 50         # cost cap
  python manage.py sync_npi_pharmacies --dry-run          # show counts only
"""
from __future__ import annotations

import re as _re


def _is_consumer_website(url: str, endpoint_meta: dict) -> bool:
    """True only if the NPPES endpoint URL looks like a public consumer site.
    Strips out HIE / XDR / FHIR / Direct-messaging URLs (the bulk of what
    NPPES actually ships under "endpoints")."""
    if not url or not url.lower().startswith(("http://", "https://")):
        return False
    low = url.lower()
    if _re.match(r"^https?://\d+\.\d+\.\d+\.\d+", low):
        return False  # bare-IP URLs are never consumer sites
    bad = (
        "/gateway/", "/nhinservice", "xdrrequest_service", "xdrresponse_service",
        "documentsubmission", "/fhir/", "fhirproxy", "fhir-proxy",
        ":8291", ":8443/services", "/soap/", "/wsdl",
        "/direct.", "directmessaging", "directtrust",
    )
    if any(s in low for s in bad):
        return False
    etype = (endpoint_meta.get("endpointType") or "").lower()
    if etype and etype not in ("other url", "otherurl", "website", "other"):
        return False
    return True

import time
from typing import Iterable

import requests
from django.core.management.base import BaseCommand
from django.db import transaction

from pharmacypulse.models import Pharmacy

API_URL = "https://npiregistry.cms.hhs.gov/api/"
API_VERSION = "2.1"
PAGE_SIZE = 200
MAX_SKIP = 1000  # API ceiling
REQUEST_TIMEOUT = 20

# Taxonomy code → (description, is_digital). The description is what NPPES
# returns; we send it via taxonomy_description filter (URL-encoded).
# NPPES `taxonomy_description` filter expects the SUFFIX after "Pharmacy, ",
# not the full description. The response contains the full
# "Pharmacy, Community/Retail Pharmacy" string, but the filter wants just
# "Community/Retail Pharmacy" — discovered via the API's error response.
TAXONOMIES = [
    ("3336C0003X", "Community/Retail Pharmacy", 0),
    ("3336M0002X", "Mail Order Pharmacy", 1),
    ("3336S0011X", "Specialty Pharmacy", 0),
    ("3336C0004X", "Compounding Pharmacy", 0),
]

US_STATES = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA",
    "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY",
    "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX",
    "UT", "VT", "VA", "WA", "WV", "WI", "WY",
]


class Command(BaseCommand):
    help = "Sync US pharmacies from the NPPES NPI Registry"

    def add_arguments(self, parser):
        parser.add_argument("--state", type=str, default="",
                            help="Two-letter state code; default = all 50 + DC")
        parser.add_argument("--zip-prefix", type=str, default="",
                            help="2-digit postal code prefix (e.g. '19' for Philly area)")
        parser.add_argument("--limit", type=int, default=0,
                            help="Stop after N pharmacies inserted (cost/test cap)")
        parser.add_argument("--dry-run", action="store_true",
                            help="Don't write to DB, just count")
        parser.add_argument("--sleep", type=float, default=0.2,
                            help="Seconds between API requests (default 0.2)")
        parser.add_argument("--stale-days", type=int, default=None,
                            help="Skip states whose newest pharmacy row was "
                                 "updated within the last N days. Lets a daily "
                                 "Heroku Scheduler job act as a quarterly refresh "
                                 "(--stale-days 90) without re-syncing fresh data.")

    def handle(self, *args, **options):
        state = (options["state"] or "").upper().strip()
        zip_prefix = options["zip_prefix"].strip()
        limit = options["limit"]
        dry_run = options["dry_run"]
        sleep = options["sleep"]
        stale_days = options["stale_days"]

        if state and zip_prefix:
            self.stdout.write(self.style.ERROR("Use --state OR --zip-prefix, not both"))
            return

        # Build the iteration plan.
        if zip_prefix:
            iteration = [{"postal_code": f"{zip_prefix}*"}]
            scope = f"ZIP {zip_prefix}*"
        elif state:
            iteration = [{"state": state}]
            scope = f"state {state}"
        else:
            iteration = [{"state": s} for s in US_STATES]
            scope = "all 50 states + DC"

        # Stale-days filter: skip states where the most recent NPPES-sourced
        # pharmacy was updated within the last N days. Lets a daily scheduler
        # job behave as a per-state quarterly refresh.
        if stale_days is not None and not zip_prefix and not state:
            from datetime import timedelta
            from django.db.models import Max
            from django.utils import timezone as djtz_local
            cutoff = djtz_local.now() - timedelta(days=stale_days)
            fresh_states = set(
                Pharmacy.objects.filter(taxonomy_code__startswith="3336")
                .values("state")
                .annotate(last_updated=Max("updated_at"))
                .filter(last_updated__gte=cutoff)
                .values_list("state", flat=True)
            )
            iteration = [it for it in iteration if it.get("state") not in fresh_states]
            self.stdout.write(
                f"Stale mode: {len(iteration)} states need refresh "
                f"({len(fresh_states)} fresh, skipped)"
            )

        self.stdout.write(f"Syncing pharmacies from NPPES — scope: {scope}")
        self.stdout.write(f"Taxonomies: {[t[0] for t in TAXONOMIES]}")
        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — no DB writes"))

        total_seen = 0
        total_new = 0
        total_updated = 0
        for filter_kwargs in iteration:
            for taxonomy_code, taxonomy_desc, is_digital in TAXONOMIES:
                results = list(self._fetch_all(
                    sleep=sleep,
                    taxonomy_description=taxonomy_desc,
                    **filter_kwargs,
                ))
                if not results:
                    continue
                # If we hit the 1200 cap on a state, recursively subdivide.
                # Tier 1: state-only query (cap 1200)
                # Tier 2: state + ZIP-2 prefix (e.g. PA + "19*")
                # Tier 3: state + ZIP-3 prefix (e.g. PA + "191*") — last resort
                # Each tier fires only when the previous one capped, so dense
                # states get the full coverage and small states stay cheap.
                if "state" in filter_kwargs and len(results) >= 1200:
                    state_code = filter_kwargs["state"]
                    self.stdout.write(
                        f"  {state_code} {taxonomy_code}: 1200-cap hit, subdividing by ZIP-2"
                    )
                    seen_npis = {r.get("number") for r in results}
                    for prefix2 in range(100):
                        sub = list(self._fetch_all(
                            sleep=sleep,
                            taxonomy_description=taxonomy_desc,
                            postal_code=f"{prefix2:02d}*",
                            state=state_code,
                        ))
                        # If a ZIP-2 itself caps, drill into ZIP-3.
                        if len(sub) >= 1200:
                            self.stdout.write(
                                f"    ZIP {prefix2:02d}*: 1200-cap hit, drilling to ZIP-3"
                            )
                            zip2_seen = {r.get("number") for r in sub}
                            for prefix3 in range(10):
                                deeper = list(self._fetch_all(
                                    sleep=sleep,
                                    taxonomy_description=taxonomy_desc,
                                    postal_code=f"{prefix2:02d}{prefix3}*",
                                    state=state_code,
                                ))
                                for r in deeper:
                                    if r.get("number") not in zip2_seen:
                                        sub.append(r)
                                        zip2_seen.add(r.get("number"))
                        for r in sub:
                            if r.get("number") not in seen_npis:
                                results.append(r)
                                seen_npis.add(r.get("number"))

                self.stdout.write(
                    f"  {filter_kwargs}: {taxonomy_code} → {len(results)} pharmacies"
                )
                total_seen += len(results)
                if dry_run:
                    continue
                for r in results:
                    if limit and total_new >= limit:
                        break
                    created, was_new = self._upsert(r, taxonomy_code, is_digital)
                    if created and was_new:
                        total_new += 1
                    elif created:
                        total_updated += 1
                if limit and total_new >= limit:
                    break
            if limit and total_new >= limit:
                break

        self.stdout.write(self.style.SUCCESS(
            f"\nDone. seen={total_seen}, inserted={total_new}, updated={total_updated}"
        ))

    # --------------------------------------------------------------
    def _fetch_all(self, *, sleep: float, **filters) -> Iterable[dict]:
        """Paginate through up to 1200 results matching `filters`. Yields raw
        provider dicts straight from NPPES."""
        for skip in range(0, MAX_SKIP + PAGE_SIZE, PAGE_SIZE):
            page = self._call_api(skip=skip, **filters)
            yield from page
            if len(page) < PAGE_SIZE:
                return
            if sleep:
                time.sleep(sleep)

    def _call_api(self, *, skip: int = 0, **filters) -> list[dict]:
        params = {
            "version": API_VERSION,
            "enumeration_type": "NPI-2",
            "limit": PAGE_SIZE,
            "skip": skip,
            **filters,
        }
        try:
            r = requests.get(API_URL, params=params, timeout=REQUEST_TIMEOUT)
            if not r.ok:
                self.stdout.write(self.style.WARNING(
                    f"NPPES returned {r.status_code} for {params}"
                ))
                return []
            data = r.json()
            return data.get("results") or []
        except (requests.RequestException, ValueError) as e:
            self.stdout.write(self.style.WARNING(f"NPPES error: {e}"))
            return []

    # --------------------------------------------------------------
    def _upsert(self, provider: dict, taxonomy_code: str, is_digital: int):
        """Insert/update a Pharmacy row from a NPPES provider dict.
        Returns (changed, was_new)."""
        npi = provider.get("number")
        if not npi:
            return (False, False)

        basic = provider.get("basic") or {}
        legal_name = (basic.get("organization_name") or "").strip()
        # Prefer "Doing Business As" name when present — that's what consumers
        # know the pharmacy as. NPPES legal name is often LLC/Inc form
        # ("1025REDLION LLC" doing business as "NATIONAL PHARMACY").
        dba = ""
        for other in provider.get("other_names") or []:
            if (other.get("type") or "").lower().startswith("doing business as"):
                dba = (other.get("organization_name") or "").strip()
                if dba:
                    break
        display_name = dba or legal_name
        if not display_name:
            return (False, False)
        # Store the FULL NPPES name with store-number suffix preserved.
        # Suffix is stripped at display time only (see _pharmacy_dict) so we
        # don't lose information here.

        # Pick the LOCATION address (first item in addresses[] per docs).
        loc_addr = None
        for addr in provider.get("addresses") or []:
            if addr.get("address_purpose") in ("LOCATION", "PRIMARY"):
                loc_addr = addr
                break
        if loc_addr is None:
            # No practice location → likely a mail-order-only pharmacy whose
            # only listed address is the mailing one. Use that as the address
            # and rely on is_digital=1 to mark it as online.
            for addr in provider.get("addresses") or []:
                if addr.get("address_purpose") == "MAILING":
                    loc_addr = addr
                    break
        if loc_addr is None:
            return (False, False)

        # Country guard — NPPES lists a few non-US territories; we already
        # filter to US ZIPs but belt-and-suspenders.
        if loc_addr.get("country_code") not in ("", "US"):
            return (False, False)

        zip5 = (loc_addr.get("postal_code") or "")[:5]
        phone = (loc_addr.get("telephone_number") or "").strip()
        # NPPES phones are E.164ish (10 digits, no formatting). Format minimally.
        if phone and phone.isdigit() and len(phone) == 10:
            phone = f"({phone[:3]}) {phone[3:6]}-{phone[6:]}"

        # Endpoints can include a website. The endpoints array contains content-
        # type tagged URLs — but most NPPES endpoints are HIE / XDR / FHIR /
        # Direct messaging targets for clinical data exchange, NOT consumer
        # pharmacy websites. We filter aggressively to avoid showing patients
        # things like https://199.119.81.30:8291/Gateway/.../XDRResponse_Service
        # We accept the endpoint only if it (a) is HTTP/S, (b) is not flagged
        # as a non-website type, and (c) doesn't match any known HIE pattern.
        website = ""
        for ep in provider.get("endpoints") or []:
            url = (ep.get("endpoint") or "").strip()
            if _is_consumer_website(url, ep):
                website = url
                break

        # Authorized official — owner, pharmacist-in-charge, president, etc.
        ao_first = (basic.get("authorized_official_first_name") or "").strip()
        ao_last = (basic.get("authorized_official_last_name") or "").strip()
        ao_name = " ".join(p for p in [ao_first.title(), ao_last.title()] if p).strip()
        ao_title = (basic.get("authorized_official_title_or_position") or "").strip()

        # Enumeration date (when this NPI was first issued — proxy for
        # 'in business since'). Format: "YYYY-MM-DD".
        enumeration_date = None
        enum_str = basic.get("enumeration_date") or ""
        if enum_str:
            try:
                from datetime import datetime as _dt
                enumeration_date = _dt.strptime(enum_str, "%Y-%m-%d").date()
            except ValueError:
                pass

        # Skip deactivated NPIs ('D') — they represent pharmacies that have
        # closed, merged, or changed ownership.
        if basic.get("status") and basic.get("status") != "A":
            return (False, False)

        # Secondary taxonomies — e.g. a retail pharmacy might also be tagged
        # as Compounding (3336C0004X). Drive service badges from this.
        secondary = []
        for t in provider.get("taxonomies") or []:
            code = (t.get("code") or "").strip()
            if code and code != taxonomy_code:
                secondary.append(code)

        # Skip if missing core fields
        new_address = (loc_addr.get("address_1") or "")[:255]
        new_city = (loc_addr.get("city") or "").title()
        new_state = (loc_addr.get("state") or "").upper()
        if not (new_address and new_city and new_state and zip5):
            return (False, False)

        # NPI is the natural key. Older Google-Places-sourced rows have
        # google place_id stored in npi_number — those have alphanumeric
        # values, while real NPIs are 10 digits. So a real-NPI lookup
        # won't collide with legacy rows.
        with transaction.atomic():
            existing = Pharmacy.objects.filter(npi_number=npi).first()
            if existing is None:
                # First-time insert — set everything from NPPES.
                Pharmacy.objects.create(
                    npi_number=npi,
                    name=display_name[:255],
                    address=new_address,
                    city=new_city,
                    state=new_state,
                    zip=zip5,
                    phone=phone,
                    website=website,
                    is_digital=is_digital,
                    status="active",
                    taxonomy_code=taxonomy_code,
                    enumeration_date=enumeration_date,
                    authorized_official_name=ao_name,
                    authorized_official_title=ao_title,
                    secondary_taxonomies=",".join(sorted(set(secondary))),
                )
                return (True, True)

            # ----- Smart merge for re-syncs -----
            # Always-update fields: NPPES-authoritative metadata (name,
            # taxonomy, owner, etc.) gets refreshed because these can
            # actually change at the registry side (rebrand, ownership
            # change, taxonomy update).
            updates = {
                "name": display_name[:255],
                "is_digital": is_digital,
                "status": "active",
                "taxonomy_code": taxonomy_code,
                "enumeration_date": enumeration_date,
                "authorized_official_name": ao_name,
                "authorized_official_title": ao_title,
                "secondary_taxonomies": ",".join(sorted(set(secondary))),
            }

            # Address change → invalidate Google enrichment.
            # If the pharmacy moved (zip changed materially, or street address
            # changed), the lat/lng we have is for the OLD location. Clear the
            # geo + place_id + reset enrichment_checked_at so the next viewer
            # triggers a fresh Google lookup. Hours and PharmacyHours rows
            # are likewise stale — but we don't delete them here; the next
            # enrichment will overwrite via update_or_create.
            address_changed = (
                existing.zip != zip5
                or existing.address != new_address
                or existing.city != new_city
                or existing.state != new_state
            )
            if address_changed:
                updates.update({
                    "address": new_address,
                    "city": new_city,
                    "state": new_state,
                    "zip": zip5,
                    "latitude": None,
                    "longitude": None,
                    "place_id": "",
                    "enrichment_checked_at": None,
                })
            # Phone + website: only fill from NPPES if currently empty.
            # Google enrichment provides nicer-formatted phones and reliable
            # websites; we don't want a re-sync to overwrite those.
            if not existing.phone and phone:
                updates["phone"] = phone
            if not existing.website and website:
                updates["website"] = website
            # Note: latitude, longitude, place_id, enrichment_checked_at, and
            # PharmacyHours rows are NEVER touched on a non-address-change
            # re-sync. That's the whole point — preserve enrichment.
            for k, v in updates.items():
                setattr(existing, k, v)
            existing.save(update_fields=list(updates.keys()) + ["updated_at"])
        return (True, False)
