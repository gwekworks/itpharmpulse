"""ZIP-level bulk map geocoding.

Puts a map pin on every pharmacy for ~$25 instead of $1,200. Strategy:
  1. Find unique ZIPs across all pharmacies missing lat/lng
  2. For each ZIP, call Google Geocoding API once → cache the result in
     the ZipCentroid table
  3. Update all pharmacies in that ZIP with the centroid

Subsequent runs are free for known ZIPs (cache hit). Lazy enrichment on
the pharmacy detail page later refines ZIP-centroid pharmacies to
street-level lat/lng + hours when actually viewed.

Cost model:
  - First run: ~5K unique US ZIPs × $0.005 = ~$25
  - Subsequent runs: $0 (all ZIPs cached)
  - Per-pharmacy: $0 (ZIP centroid already known)

Trade-off: map pins are ZIP-level accurate (5–30 mile radius depending on
density), not street-level. Good enough for "are there pharmacies in my
area" navigation. Detail-page enrichment refines to exact location.

Usage:
  python manage.py geocode_pharmacies_by_zip            # run for missing ZIPs
  python manage.py geocode_pharmacies_by_zip --refresh  # re-fetch all ZIPs
  python manage.py geocode_pharmacies_by_zip --limit 100 # cost cap
  python manage.py geocode_pharmacies_by_zip --dry-run  # show plan
"""
from __future__ import annotations

import time
from typing import Optional

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from pharmacypulse.models import Pharmacy, ZipCentroid


class Command(BaseCommand):
    help = "Backfill lat/lng on pharmacies via ZIP centroid (Google Geocoding API)"

    def add_arguments(self, parser):
        parser.add_argument("--refresh", action="store_true",
                            help="Re-geocode all ZIPs even if cached")
        parser.add_argument("--limit", type=int, default=0,
                            help="Stop after geocoding N ZIPs (cost cap)")
        parser.add_argument("--dry-run", action="store_true",
                            help="Show ZIP counts, don't call API")
        parser.add_argument("--sleep", type=float, default=0.05,
                            help="Seconds between API calls (default 0.05)")

    def handle(self, *args, **options):
        key = settings.GOOGLE_PLACES_KEY
        if not key:
            raise CommandError("GOOGLE_PLACES_KEY not configured")

        # Find ZIPs that need geocoding, ordered by pharmacy count DESC.
        # Top-N ZIPs cover the most pharmacies — geocoding the densest 5K
        # ZIPs covers ~75% of pharmacies, vs alphabetical-order which would
        # waste budget on rural ZIPs first.
        from django.db.models import Count
        zips_qs = Pharmacy.objects.filter(latitude__isnull=True).exclude(zip="")
        unique_zips = list(
            zips_qs.values("zip")
            .annotate(n=Count("id"))
            .order_by("-n")
            .values_list("zip", flat=True)
        )
        self.stdout.write(f"Pharmacies missing lat/lng: {zips_qs.count()}")
        self.stdout.write(f"Unique ZIPs to geocode: {len(unique_zips)}")

        if not options["refresh"]:
            cached = set(ZipCentroid.objects.values_list("zip", flat=True))
            unique_zips = [z for z in unique_zips if z not in cached]
            self.stdout.write(f"  After cache filter: {len(unique_zips)} new ZIPs")

        if options["limit"]:
            unique_zips = unique_zips[:options["limit"]]
            self.stdout.write(f"  Limit applied: {len(unique_zips)} ZIPs this run")

        cost_estimate = len(unique_zips) * 0.005
        self.stdout.write(f"Estimated Google Geocoding cost: ${cost_estimate:.2f}")

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("DRY RUN — no API calls made"))
            return

        resolved = 0
        failed = 0
        for i, zip_code in enumerate(unique_zips, 1):
            centroid = self._geocode_zip(zip_code, key)
            if centroid is None:
                failed += 1
                continue
            ZipCentroid.objects.update_or_create(
                zip=zip_code,
                defaults={
                    "latitude": centroid["lat"],
                    "longitude": centroid["lng"],
                    "city": centroid.get("city", "")[:64],
                    "state": centroid.get("state", "")[:2],
                },
            )
            resolved += 1
            if i % 100 == 0:
                self.stdout.write(f"  {i}/{len(unique_zips)} ZIPs done...")
            if options["sleep"]:
                time.sleep(options["sleep"])

        self.stdout.write(self.style.SUCCESS(
            f"Geocoded {resolved} ZIPs, {failed} failed."
        ))

        # Apply the centroids to all pharmacies missing lat/lng.
        self._apply_centroids_to_pharmacies()

    # ----------------------------------------------------------
    def _geocode_zip(self, zip_code: str, key: str) -> Optional[dict]:
        """Call Google Geocoding API for a ZIP. Returns lat/lng/city/state
        or None on failure. Same US-anchoring strategy as flows.ingest_zip
        (always pass 'USA' to avoid international ZIP collisions)."""
        try:
            r = requests.get(
                "https://maps.googleapis.com/maps/api/geocode/json",
                params={"address": f"{zip_code}, USA", "key": key},
                timeout=10,
            )
            data = r.json()
        except (requests.RequestException, ValueError):
            return None
        if data.get("status") != "OK":
            return None
        result = (data.get("results") or [{}])[0]
        loc = (result.get("geometry") or {}).get("location") or {}
        if not (loc.get("lat") and loc.get("lng")):
            return None
        # Verify it's actually US (Vilnius/Lithuania trap from earlier)
        out = {"lat": loc["lat"], "lng": loc["lng"]}
        for comp in result.get("address_components") or []:
            types = comp.get("types") or []
            if "country" in types:
                if comp.get("short_name") != "US":
                    return None
            elif "locality" in types:
                out["city"] = comp.get("long_name", "")
            elif "administrative_area_level_1" in types:
                out["state"] = comp.get("short_name", "")
        return out

    def _apply_centroids_to_pharmacies(self):
        """Bulk-update Pharmacy rows that have a ZIP we've geocoded but no
        lat/lng yet. Done as one UPDATE per ZIP (much faster than per-row)."""
        cached = {z.zip: z for z in ZipCentroid.objects.all()}
        total_updated = 0
        for zip_code, centroid in cached.items():
            n = Pharmacy.objects.filter(
                zip=zip_code, latitude__isnull=True,
            ).update(latitude=centroid.latitude, longitude=centroid.longitude)
            total_updated += n
        self.stdout.write(self.style.SUCCESS(
            f"Applied centroids to {total_updated} pharmacies"
        ))
