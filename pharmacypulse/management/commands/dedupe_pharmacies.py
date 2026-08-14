"""Dedupe pharmacies that share a normalized (name, address, zip).

NPPES occasionally lists the same physical pharmacy under multiple NPIs
(e.g. mid-year ownership changes, taxonomy reclassifications). The
user-reported case was Caspian Pharmacy at 19745 Ventura Blvd appearing
twice in 91364.

Strategy:
- Group by (lower(name), lower(address), zip).
- In each group with >1 row, keep the row with the most reviews; ties
  break on lowest id (oldest record, assumed more stable).
- Soft-delete the rest by setting status='duplicate' and deleted_at=now.
  We do NOT hard-delete because reviews FK to pharmacy_id.
- If the kept row is missing data the dropped row had (phone, website,
  hours), backfill from the dropped row before marking it duplicate.

Usage:
  python manage.py dedupe_pharmacies          # dry run
  python manage.py dedupe_pharmacies --apply  # actually mark duplicates
"""
from __future__ import annotations

from collections import defaultdict

from django.core.management.base import BaseCommand
from django.utils import timezone as djtz

from pharmacypulse.models import Pharmacy


def _norm(s: str | None) -> str:
    return (s or "").strip().lower()


class Command(BaseCommand):
    help = "Soft-delete duplicate pharmacy rows (same name/address/zip)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Actually mark duplicates (without this, dry run only).")

    def handle(self, *args, **options):
        groups = defaultdict(list)
        qs = Pharmacy.objects.filter(status="active", deleted_at__isnull=True)
        for p in qs.only("id", "name", "address", "zip", "total_reviews",
                          "phone", "website").iterator():
            key = (_norm(p.name), _norm(p.address), _norm(p.zip))
            if all(key):  # skip rows missing any of the three
                groups[key].append(p)

        dupe_groups = [g for g in groups.values() if len(g) > 1]
        self.stdout.write(self.style.WARNING(
            f"Found {len(dupe_groups)} duplicate groups covering "
            f"{sum(len(g) for g in dupe_groups)} rows."
        ))

        marked = 0
        for group in dupe_groups:
            # Keep row with most reviews; tie-break on lowest id (oldest).
            group.sort(key=lambda p: (-p.total_reviews, p.id))
            keeper, *dups = group
            self.stdout.write(
                f"  KEEP  [{keeper.id}] {keeper.name} ({keeper.total_reviews}rv)"
            )
            for d in dups:
                self.stdout.write(
                    f"    -> drop [{d.id}] {d.name} ({d.total_reviews}rv)"
                )
                if options["apply"]:
                    # Backfill keeper from dup if keeper is missing those fields.
                    keeper_updates = {}
                    if not keeper.phone and d.phone:
                        keeper_updates["phone"] = d.phone
                    if not keeper.website and d.website:
                        keeper_updates["website"] = d.website
                    if keeper_updates:
                        Pharmacy.objects.filter(id=keeper.id).update(**keeper_updates)
                    Pharmacy.objects.filter(id=d.id).update(
                        status="duplicate", deleted_at=djtz.now(),
                    )
                    marked += 1

        if options["apply"]:
            self.stdout.write(self.style.SUCCESS(f"Marked {marked} duplicates."))
        else:
            self.stdout.write(self.style.NOTICE(
                "\nDry run. Re-run with --apply to mark duplicates."
            ))
