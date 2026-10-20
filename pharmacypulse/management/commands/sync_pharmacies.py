"""
Nationwide pharmacy sync via Google Places API.

Usage:
  python manage.py sync_pharmacies                   # all ~160 major-city ZIPs
  python manage.py sync_pharmacies --state TX        # one state only
  python manage.py sync_pharmacies --limit 10        # first N ZIPs (cost check)
  python manage.py sync_pharmacies --radius 8000     # larger search radius
  python manage.py sync_pharmacies --stale-days 30   # only ZIPs not refreshed in 30d

API cost estimate per ZIP: ~$0.03 (nearbysearch) + ~$0.50 (details × 20 results)
Full run (~160 ZIPs): roughly $85–$100. Run --limit 5 first to validate on Benmore acct.
Stale mode: cheap daily run — only syncs ZIPs with pharmacies older than N days.
"""
from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Max
from django.utils import timezone

from pharmacypulse.flows import ingest_zip
from pharmacypulse.models import Pharmacy

# 2–4 major ZIPs per state, covering all 50 states + DC
MAJOR_ZIPS: list[tuple[str, str]] = [
    # (state, zip)
    ("AL", "35203"), ("AL", "36104"),
    ("AK", "99501"), ("AK", "99701"),
    ("AZ", "85001"), ("AZ", "85701"), ("AZ", "85201"),
    ("AR", "72201"), ("AR", "72401"),
    ("CA", "90001"), ("CA", "94102"), ("CA", "95814"), ("CA", "92101"),
    ("CO", "80202"), ("CO", "80903"),
    ("CT", "06103"), ("CT", "06510"),
    ("DC", "20001"),
    ("DE", "19801"), ("DE", "19901"),
    ("FL", "32301"), ("FL", "33101"), ("FL", "32801"), ("FL", "33601"), ("FL", "32202"),
    ("GA", "30301"), ("GA", "31401"),
    ("HI", "96813"),
    ("ID", "83701"), ("ID", "83401"),
    ("IL", "60601"), ("IL", "62701"), ("IL", "61602"),
    ("IN", "46201"), ("IN", "46601"),
    ("IA", "50301"), ("IA", "52401"),
    ("KS", "66101"), ("KS", "67202"),
    ("KY", "40201"), ("KY", "40502"),
    ("LA", "70112"), ("LA", "70801"),
    ("ME", "04101"), ("ME", "04401"),
    ("MD", "21202"), ("MD", "20601"),
    ("MA", "02108"), ("MA", "02301"), ("MA", "01103"),  # 02108 = downtown Boston (02101 is PO-box-only and Google can't pinpoint it)
    ("MI", "48226"), ("MI", "49503"), ("MI", "48823"),
    ("MN", "55401"), ("MN", "55101"), ("MN", "55802"),
    ("MS", "39201"), ("MS", "39501"),
    ("MO", "63101"), ("MO", "64108"), ("MO", "65101"),
    ("MT", "59601"), ("MT", "59801"),
    ("NE", "68102"), ("NE", "68501"),
    ("NV", "89101"), ("NV", "89501"),
    ("NH", "03101"), ("NH", "03301"),
    ("NJ", "07102"), ("NJ", "08608"), ("NJ", "07601"),
    ("NM", "87101"), ("NM", "87501"),
    ("NY", "10001"), ("NY", "14201"), ("NY", "13201"), ("NY", "12201"), ("NY", "10451"),
    ("NC", "27601"), ("NC", "28201"), ("NC", "27401"),
    ("ND", "58501"), ("ND", "58102"),
    ("OH", "44101"), ("OH", "43201"), ("OH", "45201"),
    ("OK", "73102"), ("OK", "74103"),
    ("OR", "97201"), ("OR", "97401"),
    ("PA", "19107"), ("PA", "15222"), ("PA", "17101"),
    ("RI", "02903"), ("RI", "02840"),
    ("SC", "29201"), ("SC", "29401"),
    ("SD", "57501"), ("SD", "57104"),
    ("TN", "37201"), ("TN", "38101"), ("TN", "37902"),
    ("TX", "78701"), ("TX", "77002"), ("TX", "75201"), ("TX", "78201"), ("TX", "79901"),
    ("UT", "84101"), ("UT", "84401"),
    ("VT", "05401"), ("VT", "05602"),
    ("VA", "23219"), ("VA", "23510"), ("VA", "22201"),
    ("WA", "98101"), ("WA", "99201"), ("WA", "98402"),
    ("WV", "25301"), ("WV", "26330"),
    ("WI", "53201"), ("WI", "53703"), ("WI", "54601"),
    ("WY", "82001"), ("WY", "82601"), ("WY", "82801"), ("WY", "82901"),  # Sheridan + Rock Springs added
    # Secondary ZIPs for sparse states (added 2026-05 to fill in coverage)
    ("SD", "57701"), ("SD", "57401"),  # Rapid City + Aberdeen
    ("MT", "59101"), ("MT", "59715"),  # Billings + Bozeman
    ("AK", "99801"), ("AK", "99901"),  # Juneau + Ketchikan
    ("VT", "05701"),                   # Rutland
    ("WV", "25801"), ("WV", "26505"),  # Beckley + Morgantown
]


class Command(BaseCommand):
    help = "Sync pharmacies nationwide via Google Places API"

    def add_arguments(self, parser):
        parser.add_argument("--state", type=str, help="Only sync ZIPs for this state (e.g. TX)")
        parser.add_argument("--limit", type=int, help="Stop after N ZIPs (useful for cost checks)")
        parser.add_argument("--radius", type=str, default="5000", help="Search radius in metres (default 5000)")
        parser.add_argument("--stale-days", type=int, default=None,
                            help="Only sync ZIPs whose pharmacies haven't been updated in N days")

    def handle(self, *args, **options):
        key = settings.GOOGLE_PLACES_KEY
        if not key:
            raise CommandError("GOOGLE_PLACES_KEY is not configured in settings / .env")

        state_filter = (options["state"] or "").upper()
        limit = options["limit"]
        radius = options["radius"]
        stale_days = options["stale_days"]

        zips = [(s, z) for s, z in MAJOR_ZIPS if not state_filter or s == state_filter]
        if not zips:
            raise CommandError(f"No ZIPs found for state '{state_filter}'")

        if stale_days is not None:
            cutoff = timezone.now() - timedelta(days=stale_days)
            fresh_zips = set(
                Pharmacy.objects.values("zip")
                .annotate(last_updated=Max("updated_at"))
                .filter(last_updated__gte=cutoff)
                .values_list("zip", flat=True)
            )
            zips = [(s, z) for s, z in zips if z not in fresh_zips]
            self.stdout.write(f"Stale mode: {len(zips)} ZIPs need refresh (>{stale_days}d old)")

        if limit:
            zips = zips[:limit]

        self.stdout.write(f"Syncing {len(zips)} ZIP codes (radius={radius}m)...")
        total_inserted = 0
        total_found = 0
        errors = 0

        for i, (state, zip_code) in enumerate(zips, 1):
            self.stdout.write(f"  [{i}/{len(zips)}] {state} {zip_code} ... ", ending="")
            self.stdout.flush()
            result = ingest_zip(key, zip_code, radius, state=state)
            if result["error"]:
                self.stdout.write(self.style.WARNING(f"SKIP — {result['error']}"))
                errors += 1
            else:
                self.stdout.write(
                    self.style.SUCCESS(f"{result['total']} found, {result['inserted']} new")
                )
                total_inserted += result["inserted"]
                total_found += result["total"]

        self.stdout.write(
            self.style.SUCCESS(
                f"\nDone. {total_found} total results, {total_inserted} new pharmacies inserted, {errors} ZIP errors."
            )
        )
