"""End-to-end pharmacy refresh — chains together NPPES sync, chain
detection, and ZIP geocoding in one Heroku Scheduler-friendly job.

Each step is idempotent + self-skipping when nothing has changed:

  1. sync_npi_pharmacies --stale-days 90
       NPPES API. Skips states where every row was updated within the last
       90 days. Most days: 0 states need refresh, exits in seconds. Once a
       quarter: a handful of states actually re-sync (5-15 min). Smart-merge
       preserves Google enrichment data unless a pharmacy actually moved.

  2. compute_chain_size
       Pure DB aggregate. Runs in 1-2 seconds regardless of data size.
       Refreshes 'Chain · X loc.' badges so newly-added pharmacies count.

  3. geocode_pharmacies_by_zip --limit 500
       Google Geocoding API. Skips ZIPs already cached in ZipCentroid table
       (so most days = 0 calls). Caps at 500 new ZIPs per run = ~$2.50 max
       so a runaway sync can't blow the bill. Cumulative full coverage
       happens over multiple days as new ZIPs trickle in.

  4. sync_publish_pharmacies
       Insert-only backfill of new pharmacies → pharmacies_publishable, the
       table the public search pages query. Never overwrites existing rows,
       so manual edits (name, publish_status, etc.) are preserved.

Daily Heroku Scheduler job: `python manage.py refresh_pharmacies`
For an hourly cadence, run the NPPES step + publish step together (the
NPPES sync is cheap/idempotent and self-skips when nothing changed):
  python manage.py sync_npi_pharmacies --stale-days 1
  python manage.py sync_publish_pharmacies
Cost ceiling per day: ~$2.50. Idle days: $0.

Use --skip-X to selectively disable a step (e.g. for debugging).
"""
from __future__ import annotations

import time

from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Run the full pharmacy refresh pipeline (NPPES + chain + geocode)"

    def add_arguments(self, parser):
        parser.add_argument("--skip-nppes", action="store_true",
                            help="Skip the NPPES NPI Registry sync step")
        parser.add_argument("--skip-chain", action="store_true",
                            help="Skip the chain_size aggregate step")
        parser.add_argument("--skip-geocode", action="store_true",
                            help="Skip the ZIP geocoding step")
        parser.add_argument("--stale-days", type=int, default=90,
                            help="Per-state NPPES re-sync threshold (default 90)")
        parser.add_argument("--geocode-limit", type=int, default=500,
                            help="Max new ZIPs to geocode per run (default 500, cost cap)")
        parser.add_argument("--skip-publish", action="store_true",
                            help="Skip the pharmacies_publishable rebuild step")

    def handle(self, *args, **options):
        steps = []

        # ---- 1. NPPES sync ----
        if not options["skip_nppes"]:
            t0 = time.time()
            self.stdout.write("\n=== Step 1/4: NPPES sync ===")
            try:
                call_command(
                    "sync_npi_pharmacies",
                    stale_days=options["stale_days"],
                )
                steps.append(("NPPES sync", time.time() - t0, "ok"))
            except Exception as e:  # noqa: BLE001
                self.stdout.write(self.style.ERROR(f"NPPES sync failed: {e}"))
                steps.append(("NPPES sync", time.time() - t0, f"failed: {e}"))

        # ---- 2. Chain size ----
        if not options["skip_chain"]:
            t0 = time.time()
            self.stdout.write("\n=== Step 2/4: chain_size aggregate ===")
            try:
                call_command("compute_chain_size")
                steps.append(("chain_size", time.time() - t0, "ok"))
            except Exception as e:  # noqa: BLE001
                self.stdout.write(self.style.ERROR(f"chain_size failed: {e}"))
                steps.append(("chain_size", time.time() - t0, f"failed: {e}"))

        # ---- 3. ZIP geocoding ----
        if not options["skip_geocode"]:
            t0 = time.time()
            self.stdout.write("\n=== Step 3/4: ZIP geocoding ===")
            try:
                call_command(
                    "geocode_pharmacies_by_zip",
                    limit=options["geocode_limit"],
                )
                steps.append(("ZIP geocode", time.time() - t0, "ok"))
            except Exception as e:  # noqa: BLE001
                self.stdout.write(self.style.ERROR(f"ZIP geocode failed: {e}"))
                steps.append(("ZIP geocode", time.time() - t0, f"failed: {e}"))

        # ---- 4. Publish ----
        if not options["skip_publish"]:
            t0 = time.time()
            self.stdout.write("\n=== Step 4/4: Backfill pharmacies_publishable ===")
            try:
                call_command("sync_publish_pharmacies")
                steps.append(("publishable", time.time() - t0, "ok"))
            except Exception as e:  # noqa: BLE001
                self.stdout.write(self.style.ERROR(f"publishable failed: {e}"))
                steps.append(("publishable", time.time() - t0, f"failed: {e}"))

        # Summary
        self.stdout.write("\n=== Refresh summary ===")
        for name, elapsed, status in steps:
            self.stdout.write(f"  {name:18}  {elapsed:6.1f}s  {status}")
