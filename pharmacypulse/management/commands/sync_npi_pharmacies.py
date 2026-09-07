"""Sync US pharmacies from the NPPES NPI Registry.

NPPES (CMS) is the authoritative source for every NPI-registered pharmacy
in the US. We query BOTH enumeration types:

  - NPI-2 (organizations) — the classic pharmacy rows (chains, independents).
  - NPI-1 (individuals)   — sole-proprietor pharmacists who registered their
    own NPI. Only kept when they actually carry a pharmacy taxonomy code
    (3336…); pharmacists / pharmacy technicians (1835…, 1837…) are skipped.

API: https://npiregistry.cms.hhs.gov/api/?version=2.1
Free, no auth. Returns up to 200 results per request, skip up to 1000 →
max 1200 records per filter combo.

Strategy: paginate per ZIP-2 prefix (00..99) per pharmacy taxonomy. Postal
codes don't cross state lines (mostly), and each ZIP-2 area has fewer
pharmacies than a full state — keeps us under the 1200-per-query cap even
in dense regions.

Pharmacy taxonomies pulled (NUCC code → our `is_digital` flag):
  3336C0003X  Community/Retail Pharmacy     → is_digital=0
  3336M0002X  Mail Order Pharmacy           → is_digital=1
  3336S0011X  Specialty Pharmacy            → is_digital=0
  3336C0004X  Compounding Pharmacy          → is_digital=0
  3336L0003X  Long Term Care Pharmacy       → is_digital=0
  3336H0001X  Home Infusion Therapy Pharm.  → is_digital=0
  3336C0002X  Clinic Pharmacy               → is_digital=0
  333600000X  Pharmacy (generic)            → is_digital=0

Usage:
  python manage.py sync_npi_pharmacies                    # full US sync
  python manage.py sync_npi_pharmacies --zip-prefix 19    # one prefix
  python manage.py sync_npi_pharmacies --state MD --limit 10 --debug
  python manage.py sync_npi_pharmacies --limit 50         # cost cap
  python manage.py sync_npi_pharmacies --dry-run          # show counts only
"""
from __future__ import annotations

import re as _re
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

# Taxonomy code → (filter description, is_digital). The description is what we
# send to NPPES' `taxonomy_description` filter. That filter expects the SUFFIX
# after "Pharmacy, " — NOT the full "Pharmacy, Community/Retail Pharmacy"
# string (discovered via the API's error response).
TAXONOMIES = [
    ("3336C0003X", "Community/Retail Pharmacy", 0),
    ("3336M0002X", "Mail Order Pharmacy", 1),
    ("3336S0011X", "Specialty Pharmacy", 0),
    ("3336C0004X", "Compounding Pharmacy", 0),
    ("3336L0003X", "Long Term Care Pharmacy", 0),
    ("3336H0001X", "Home Infusion Therapy Pharmacy", 0),
    ("3336C0002X", "Clinic Pharmacy", 0),
    ("333600000X", "Pharmacy", 0),
]

# Legitimate pharmacy taxonomies — the ONLY codes that mean "this provider IS
# a pharmacy". Used to filter the raw response so NPI-1 individuals (whose
# own taxonomy is often "Pharmacist" / "Pharmacy Technician") don't slip in.
PHARMACY_TAXONOMIES = {
    "3336C0003X",  # Community/Retail Pharmacy
    "3336M0002X",  # Mail Order Pharmacy
    "3336S0011X",  # Specialty Pharmacy
    "3336C0004X",  # Compounding Pharmacy
    "3336L0003X",  # Long Term Care Pharmacy
    "3336H0001X",  # Home Infusion Therapy Pharmacy
    "3336C0002X",  # Clinic Pharmacy
    "333600000X",  # Pharmacy (generic)
}

# Taxonomies that look pharmacy-adjacent but are NOT a pharmacy.
NON_PHARMACY_TAXONOMIES = {
    "183700000X",  # Pharmacy Technician
    "183500000X",  # Pharmacist (individual practitioner)
    "332B00000X",  # Durable Medical Equipment
    "332900000X",  # Non-Pharmacy Dispensing Site
}

# Code → is_digital lookup for deriving the flag from a provider's PRIMARY
# taxonomy (not from whichever filter surfaced the row).
_TAX_DIGITAL = {code: is_digital for code, _, is_digital in TAXONOMIES}

# Enumeration types, organizations first (the bulk of real pharmacies), then
# sole-proprietor individuals.
ENUMERATION_TYPES = ["NPI-2", "NPI-1"]

US_STATES = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA",
    "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY",
    "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX",
    "UT", "VT", "VA", "WA", "WV", "WI", "WY",
]


def _is_consumer_website(url: str, endpoint_meta: dict) -> bool:
    """True only if the NPPES endpoint URL looks like a public consumer site.
    Strips out HIE / XDR / FHIR / Direct-messaging / SOAP / API endpoints
    (the bulk of what NPPES actually ships under "endpoints"), plus bare-IP
    and machine-oriented URLs."""
    if not url or not url.lower().startswith(("http://", "https://")):
        return False
    low = url.lower()
    if _re.match(r"^https?://\d+\.\d+\.\d+\.\d+", low):
        return False  # bare-IP URLs are never consumer sites
    # endpointType whitelist — anything not flagged as a website is clinical
    # data-exchange infra (CONNECT, DIRECT, FHIR, XDR, XDS, SOAP, REST, ...).
    etype = (endpoint_meta.get("endpointType") or "").lower()
    if etype and etype not in ("other url", "otherurl", "website", "other"):
        return False
    # `use` tag — reject HIE / Direct messaging regardless of type.
    use = (endpoint_meta.get("use") or "").upper()
    if use in ("HIE", "DIRECT", "XDR", "XDS", "SOAP"):
        return False
    bad = (
        "/gateway/", "/nhinservice", "xdrrequest", "xdrresponse",
        "documentsubmission", "/fhir/", "fhirproxy", "fhir-proxy",
        ":8291", ":8443/services", "/soap/", "/wsdl",
        "/direct.", "directmessaging", "directtrust",
        "/api/", "/rest/", ".xml", ".json", ".wsdl",
        "hl7", "cda", "xds",
    )
    if any(s in low for s in bad):
        return False
    return True


class Command(BaseCommand):
    help = "Sync US pharmacies from the NPPES NPI Registry (NPI-1 + NPI-2)"

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
        parser.add_argument("--debug", action="store_true",
                            help="Print every API request + per-filter counts")
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
        self.debug = options["debug"]

        self.metrics = {
            "total_fetched": 0,
            "npi_1_fetched": 0,
            "npi_2_fetched": 0,
            "skipped_deactivated": 0,
            "skipped_non_us": 0,
            "skipped_missing_data": 0,
            "skipped_non_pharmacy": 0,
            "skipped_no_npi": 0,
            "successful_inserts": 0,
            "successful_updates": 0,
            "duplicates_found": 0,
            "errors": 0,
        }
        # NPIs already upserted this run — the same pharmacy matches several
        # taxonomy filters (e.g. retail + compounding), so process it once.
        self._seen_npis: set[str] = set()

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
        self.stdout.write(f"Enumeration types: {ENUMERATION_TYPES}")
        self.stdout.write(f"Taxonomies: {[t[0] for t in TAXONOMIES]}")
        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — no DB writes"))
        if self.debug:
            self.stdout.write(self.style.WARNING("DEBUG MODE — verbose output"))

        total_new = 0
        total_updated = 0
        for enumeration_type in ENUMERATION_TYPES:
            if limit and total_new >= limit:
                break
            self.stdout.write(f"Fetching {enumeration_type} providers...")
            for filter_kwargs in iteration:
                for taxonomy_code, taxonomy_desc, _is_digital in TAXONOMIES:
                    if limit and total_new >= limit:
                        break
                    results = list(self._fetch_all(
                        sleep=sleep,
                        enumeration_type=enumeration_type,
                        taxonomy_description=taxonomy_desc,
                        **filter_kwargs,
                    ))
                    if not results:
                        continue
                    # If we hit the 1200 cap on a state, recursively subdivide.
                    # Tier 1: state-only query (cap 1200)
                    # Tier 2: state + ZIP-2 prefix (e.g. PA + "19*")
                    # Tier 3: state + ZIP-3 prefix (e.g. PA + "191*") — last resort
                    if "state" in filter_kwargs and len(results) >= 1200:
                        state_code = filter_kwargs["state"]
                        self.stdout.write(
                            f"  {enumeration_type} {state_code} {taxonomy_code}: "
                            f"1200-cap hit, subdividing by ZIP-2"
                        )
                        seen_npis = {r.get("number") for r in results}
                        for prefix2 in range(100):
                            sub = list(self._fetch_all(
                                sleep=sleep,
                                enumeration_type=enumeration_type,
                                taxonomy_description=taxonomy_desc,
                                postal_code=f"{prefix2:02d}*",
                                state=state_code,
                            ))
                            # If a ZIP-2 itself caps, drill into ZIP-3.
                            if len(sub) >= 1200:
                                self.stdout.write(
                                    f"    ZIP {prefix2:02d}*: 1200-cap hit, "
                                    f"drilling to ZIP-3"
                                )
                                zip2_seen = {r.get("number") for r in sub}
                                for prefix3 in range(10):
                                    deeper = list(self._fetch_all(
                                        sleep=sleep,
                                        enumeration_type=enumeration_type,
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

                    # Response-side filter: only keep providers that actually
                    # carry a pharmacy taxonomy. Kills NPI-1 pharmacists /
                    # pharmacy technicians and stray non-pharmacy orgs.
                    filtered = []
                    for r in results:
                        if self._is_pharmacy_provider(r):
                            filtered.append(r)
                        else:
                            self.metrics["skipped_non_pharmacy"] += 1

                    self._log(
                        f"  {enumeration_type} {filter_kwargs} {taxonomy_code}: "
                        f"{len(results)} fetched, {len(filtered)} pharmacies"
                    )
                    if dry_run:
                        continue
                    for r in filtered:
                        if limit and total_new >= limit:
                            break
                        # Derive the primary taxonomy + is_digital from the
                        # provider itself, NOT the filter that surfaced it, so
                        # a row matching several taxonomy filters keeps the
                        # right type (e.g. a mail-order pharmacy stays digital).
                        primary = self._get_primary_taxonomy(r)
                        if not primary:
                            self.metrics["skipped_non_pharmacy"] += 1
                            continue
                        changed, was_new = self._upsert(
                            r, primary, _TAX_DIGITAL.get(primary, 0))
                        if was_new:
                            total_new += 1
                        elif changed:
                            total_updated += 1
                if limit and total_new >= limit:
                    break
            if limit and total_new >= limit:
                break

        self.stdout.write(self.style.SUCCESS(
            f"\nDone. seen={self.metrics['total_fetched']}, "
            f"inserted={total_new}, updated={total_updated}"
        ))
        for key, value in self.metrics.items():
            self.stdout.write(f"  {key}: {value}")

    # --------------------------------------------------------------
    def _log(self, msg: str):
        if self.debug:
            self.stdout.write(msg)

    def _is_pharmacy_provider(self, provider: dict) -> bool:
        """True when the provider carries at least one pharmacy taxonomy.
        Filters out NPI-1 pharmacists / pharmacy technicians and any
        non-pharmacy organization that matched a description filter."""
        for t in provider.get("taxonomies") or []:
            if (t.get("code") or "").strip() in PHARMACY_TAXONOMIES:
                return True
        return False

    def _get_primary_taxonomy(self, provider: dict) -> str:
        """The provider's own primary taxonomy when it's a pharmacy code —
        otherwise the first pharmacy taxonomy on the row. Lets us record the
        correct taxonomy regardless of which description filter matched."""
        taxonomies = provider.get("taxonomies") or []
        for t in taxonomies:
            code = (t.get("code") or "").strip()
            if t.get("primary") and code in PHARMACY_TAXONOMIES:
                return code
        for t in taxonomies:
            code = (t.get("code") or "").strip()
            if code in PHARMACY_TAXONOMIES:
                return code
        return ""

    def _get_provider_name(self, provider: dict, basic: dict) -> tuple[str, str, str]:
        """Return (display_name, legal_name, provider_type).

        NPI-1 (individual): "John Smith, PharmD" — the credential rides along
        in the display name (the Pharmacy model has no separate credential
        column). DBA wins when present.
        NPI-2 (organization): DBA name preferred, else legal name."""
        first = (basic.get("first_name") or "").strip()
        last = (basic.get("last_name") or "").strip()
        org = (basic.get("organization_name") or "").strip()

        if first and last:
            full_name = f"{first} {last}".strip()
            cred = (basic.get("credential") or "").strip()
            display = f"{full_name}, {cred}" if cred else full_name
            dba = self._get_dba(provider)
            return (dba or display), full_name, "individual"
        return (self._get_dba(provider) or org), org, "organization"

    def _get_dba(self, provider: dict) -> str:
        """The "Doing Business As" name — what consumers know the pharmacy as.
        NPPES legal names are often LLC/Inc form ("1025REDLION LLC" doing
        business as "NATIONAL PHARMACY"). Individuals can carry a DBA in
        other_names too."""
        for other in provider.get("other_names") or []:
            if (other.get("type") or "").lower().startswith("doing business as"):
                name = (other.get("organization_name") or "").strip()
                if not name:
                    name = " ".join(p for p in (
                        (other.get("first_name") or "").strip(),
                        (other.get("last_name") or "").strip(),
                    ) if p).strip()
                if name:
                    return name
        return ""

    def _get_location_address(self, provider: dict) -> dict | None:
        """The LOCATION address: prefer the explicitly-tagged LOCATION entry,
        fall back to MAILING (some mail-order-only pharmacies only list that),
        then to the first address."""
        addresses = provider.get("addresses") or []
        for addr in addresses:
            if addr.get("address_purpose") in ("LOCATION", "PRIMARY"):
                return addr
        for addr in addresses:
            if addr.get("address_purpose") == "MAILING":
                return addr
        return addresses[0] if addresses else None

    def _get_website(self, provider: dict) -> str:
        for ep in provider.get("endpoints") or []:
            url = (ep.get("endpoint") or "").strip()
            if _is_consumer_website(url, ep):
                return url
        return ""

    def _get_secondary_taxonomies(self, provider: dict, primary_code: str) -> list:
        """Secondary taxonomy codes that are actually pharmacy services
        (retail + compounding = Compounding badge), skipping stray provider
        codes like pharmacist/technician."""
        secondary = []
        for t in provider.get("taxonomies") or []:
            code = (t.get("code") or "").strip()
            if code and code != primary_code and code in PHARMACY_TAXONOMIES:
                secondary.append(code)
        return secondary

    def _get_authorized_official(self, basic: dict, provider_type: str) -> tuple[str, str]:
        """(name, title) — orgs use their authorized_official_* fields; for an
        individual the provider IS the official, so their name humanizes the
        "Owned by" line on the detail page."""
        if provider_type == "individual":
            name = " ".join(p for p in (
                (basic.get("first_name") or "").title(),
                (basic.get("last_name") or "").title(),
            ) if p).strip()
            return name, ""
        ao_first = (basic.get("authorized_official_first_name") or "").strip()
        ao_last = (basic.get("authorized_official_last_name") or "").strip()
        ao_name = " ".join(p for p in [ao_first.title(), ao_last.title()] if p).strip()
        ao_title = (basic.get("authorized_official_title_or_position") or "").strip()
        return ao_name, ao_title

    @staticmethod
    def _parse_enumeration_date(enum_str: str):
        """'YYYY-MM-DD' → date (when this NPI was issued — proxy for
        'in business since')."""
        if not enum_str:
            return None
        try:
            from datetime import datetime as _dt
            return _dt.strptime(enum_str, "%Y-%m-%d").date()
        except ValueError:
            return None

    # --------------------------------------------------------------
    def _fetch_all(self, *, sleep: float, enumeration_type: str, **filters) -> Iterable[dict]:
        """Paginate through up to 1200 results matching `filters`. Yields raw
        provider dicts straight from NPPES."""
        for skip in range(0, MAX_SKIP + PAGE_SIZE, PAGE_SIZE):
            page = self._call_api(skip=skip, enumeration_type=enumeration_type, **filters)
            yield from page
            if len(page) < PAGE_SIZE:
                return
            if sleep:
                time.sleep(sleep)

    def _call_api(self, *, skip: int = 0, enumeration_type: str = "NPI-2", **filters) -> list[dict]:
        params = {
            "version": API_VERSION,
            "enumeration_type": enumeration_type,
            "limit": PAGE_SIZE,
            "skip": skip,
            **filters,
        }
        self._log(f"  GET {API_URL}?{requests.compat.urlencode(params)}")
        try:
            r = requests.get(API_URL, params=params, timeout=REQUEST_TIMEOUT)
            if not r.ok:
                self.stdout.write(self.style.WARNING(
                    f"NPPES returned {r.status_code} for {params}"
                ))
                self.metrics["errors"] += 1
                return []
            data = r.json()
            results = data.get("results") or []
        except (requests.RequestException, ValueError) as e:
            self.stdout.write(self.style.WARNING(f"NPPES error: {e}"))
            self.metrics["errors"] += 1
            return []
        self.metrics["total_fetched"] += len(results)
        if enumeration_type == "NPI-1":
            self.metrics["npi_1_fetched"] += len(results)
        else:
            self.metrics["npi_2_fetched"] += len(results)
        return results

    # --------------------------------------------------------------
    def _upsert(self, provider: dict, taxonomy_code: str, is_digital: int):
        """Insert/update a Pharmacy row from a NPPES provider dict.
        Returns (changed, was_new)."""
        npi = provider.get("number")
        if not npi:
            self.metrics["skipped_no_npi"] += 1
            return (False, False)
        if npi in self._seen_npis:
            # Already upserted under another taxonomy filter this run.
            self.metrics["duplicates_found"] += 1
            return (False, False)
        self._seen_npis.add(npi)

        basic = provider.get("basic") or {}
        display_name, legal_name, provider_type = self._get_provider_name(provider, basic)
        if not display_name:
            self.metrics["skipped_missing_data"] += 1
            return (False, False)

        # Skip deactivated NPIs ('D') — pharmacies that have closed, merged,
        # or changed ownership.
        if basic.get("status") and basic.get("status") != "A":
            self.metrics["skipped_deactivated"] += 1
            return (False, False)

        loc_addr = self._get_location_address(provider)
        if loc_addr is None:
            self.metrics["skipped_missing_data"] += 1
            return (False, False)

        # Country guard — NPPES lists a few non-US territories; we already
        # filter to US ZIPs but belt-and-suspenders.
        if loc_addr.get("country_code") not in ("", "US"):
            self.metrics["skipped_non_us"] += 1
            return (False, False)

        # Leading 5 digits of the postal code — pharmacies store ZIP-5 only.
        zip5 = (loc_addr.get("postal_code") or "")[:5]
        # NPPES returns phone numbers as-is; use them unchanged.
        phone = (loc_addr.get("telephone_number") or "").strip()
        website = self._get_website(provider)
        secondary = self._get_secondary_taxonomies(provider, taxonomy_code)
        enumeration_date = self._parse_enumeration_date(basic.get("enumeration_date"))
        ao_name, ao_title = self._get_authorized_official(basic, provider_type)

        # Skip if missing core fields.
        new_address = (loc_addr.get("address_1") or "")[:255]
        new_city = (loc_addr.get("city") or "").title()
        new_state = (loc_addr.get("state") or "").upper()
        if not (new_address and new_city and new_state and zip5):
            self.metrics["skipped_missing_data"] += 1
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
                self.metrics["successful_inserts"] += 1
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
        self.metrics["successful_updates"] += 1
        return (True, False)