"""Recompute Pharmacy.chain_size from the normalized DBA name distribution.

chain_size = count of pharmacies sharing the same normalized DBA name.
Drives the 'Chain · X loc.' vs 'Independent' badge in the UI.

The stored `name` field keeps the FULL NPPES name (including store-number
suffixes like '#08910' or '10-0049') for internal traceability. Suffix
stripping happens at display time via _pharmacy_dict — see
pharmacypulse.text_utils.strip_chain_suffix.

Idempotent: re-running on unchanged data is a no-op.

Run after each NPPES sync, or whenever names change:
  python manage.py compute_chain_size
"""
from __future__ import annotations

from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction

from pharmacypulse.models import Pharmacy
from pharmacypulse.text_utils import strip_chain_suffix


class Command(BaseCommand):
    help = "Recompute chain_size on every Pharmacy row using normalized DBA name"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Show top chains without writing")

    def handle(self, *args, **options):
        groups: dict[str, list[int]] = defaultdict(list)
        for pid, name in Pharmacy.objects.values_list("id", "name").iterator():
            key = strip_chain_suffix(name).upper()
            if not key:
                continue
            groups[key].append(pid)

        # Top 15 preview
        top = sorted(groups.items(), key=lambda kv: -len(kv[1]))[:15]
        self.stdout.write("Top 15 chains by normalized name:")
        for norm_name, ids in top:
            self.stdout.write(f"  {len(ids):5}  {norm_name}")

        if options["dry_run"]:
            return

        with transaction.atomic():
            for ids in groups.values():
                size = len(ids)
                Pharmacy.objects.filter(id__in=ids).update(chain_size=size)

        self.stdout.write(self.style.SUCCESS(
            f"\nDone. Updated chain_size on {sum(len(v) for v in groups.values())} rows "
            f"({len(groups)} unique normalized names)."
        ))
