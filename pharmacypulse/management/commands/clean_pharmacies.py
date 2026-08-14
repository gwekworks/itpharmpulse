"""Remove non-retail-pharmacy entries from the directory.

Google Places `type=pharmacy` returns vet hospitals (which often have an
attached pharmacy) and individual practitioners listed by personal name
('Holladay Holly H', 'Smith James D, PharmD'). Patients searching for a
pharmacy near them want neither.

Filtering rules match flows._is_excluded_pharmacy(); this command applies
the same logic to existing rows.

Usage:
  python manage.py clean_pharmacies          # show what would be deleted
  python manage.py clean_pharmacies --apply  # actually delete
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from pharmacypulse.flows import _is_excluded_pharmacy
from pharmacypulse.models import Pharmacy, PharmacyHours


class Command(BaseCommand):
    help = "Remove vet hospitals and individual practitioners from the pharmacy directory"

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Actually delete (without this, just lists matches)")

    def handle(self, *args, **options):
        bad_ids = []
        sample = []
        for p in Pharmacy.objects.values("id", "name").iterator():
            # Types aren't stored on the row, so name-only check. Catches the
            # bulk of false positives (vet keywords + 3-word individual names +
            # RPH/PharmD suffixes). Place-types-based exclusions only apply
            # going forward via the ingest filter.
            if _is_excluded_pharmacy(p["name"]):
                bad_ids.append(p["id"])
                if len(sample) < 20:
                    sample.append(f'  [{p["id"]}] {p["name"]}')

        self.stdout.write(self.style.WARNING(f"Found {len(bad_ids)} non-pharmacy rows."))
        for s in sample:
            self.stdout.write(s)
        if len(bad_ids) > len(sample):
            self.stdout.write(f"  ... and {len(bad_ids) - len(sample)} more")

        if not options["apply"]:
            self.stdout.write(self.style.NOTICE("\nDry run. Re-run with --apply to delete."))
            return

        # Cascade-delete dependent rows that don't auto-cascade.
        PharmacyHours.objects.filter(pharmacy_id__in=bad_ids).delete()
        deleted = Pharmacy.objects.filter(id__in=bad_ids).delete()
        self.stdout.write(self.style.SUCCESS(f"Deleted: {deleted}"))
        self.stdout.write(self.style.SUCCESS(
            f"Remaining pharmacies: {Pharmacy.objects.count()}"
        ))
