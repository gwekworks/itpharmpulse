"""Backfill/refresh the public search table `pharmacies_publishable` from
`pharmacies`.

Public search/browse pages read from `pharmacies_publishable` (filtered to
publish_status='published'), so the live API-synced `pharmacies` table stays
authoritative while the publishable table is the editable, moderation-aware
copy the public actually sees.

How it works (idempotent, run after every pharmacy API sync):
  * Missing pharmacies are created (publish_status='published').
  * Existing publishable rows are refreshed from the source ONLY when the
    admin has never edited them (admin_edited=False). This keeps untouched
    copies in sync with the latest API data.
  * Rows the admin HAS edited (admin_edited=True) are NEVER touched — any
    curator adjustments (name, publish_status, etc.) are preserved.

Usage:
  python manage.py sync_publish_pharmacies          # backfill + refresh untouched
  python manage.py sync_publish_pharmacies --state PA
  python manage.py sync_publish_pharmacies --dry-run
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from pharmacypulse.models import Pharmacy, PharmacyPublishable

# Columns copied across from the source pharmacy.
_COPY_FIELDS = [
    "name", "address", "city", "state", "zip", "phone",
    "latitude", "longitude", "stock_confidence", "avg_wait_time",
    "avg_service_rating", "total_reviews", "is_digital", "website",
    "npi_number", "place_id", "taxonomy_code", "enumeration_date",
    "authorized_official_name", "authorized_official_title",
    "secondary_taxonomies", "chain_size", "claimed_by", "org_id",
    "phone_wait_time", "delivery_wait_time", "delivery_rating",
    "customer_service_hours", "listing_completed_at",
]


class Command(BaseCommand):
    help = "Backfill missing and refresh untouched pharmacies_publishable rows"

    def add_arguments(self, parser):
        parser.add_argument("--state", type=str, default="",
                            help="Only publish pharmacies in this state (e.g. PA)")
        parser.add_argument("--dry-run", action="store_true",
                            help="Show what would change without writing")

    def handle(self, *args, **options):
        state = (options["state"] or "").upper()
        dry_run = options["dry_run"]

        src_qs = Pharmacy.objects.filter(status="active", deleted_at__isnull=True)
        if state:
            src_qs = src_qs.filter(state=state)

        existing = {
            r.pharmacy_id: r
            for r in PharmacyPublishable.objects.all().iterator()
        }

        to_create, to_refresh, skipped_edited = [], [], 0
        for src in src_qs.iterator():
            data = {f: getattr(src, f) for f in _COPY_FIELDS}
            data["status"] = "active"
            row = existing.get(src.id)
            if row is None:
                data["publish_status"] = "published"
                to_create.append(PharmacyPublishable(pharmacy_id=src.id, **data))
            elif row.admin_edited:
                skipped_edited += 1  # admin owns this row → never overwrite
            else:
                for field, value in data.items():
                    setattr(row, field, value)
                to_refresh.append(row)

        if dry_run:
            self.stdout.write(
                f"Would create {len(to_create)}, refresh {len(to_refresh)}, "
                f"leave {skipped_edited} admin-edited rows untouched.")
            return

        with transaction.atomic():
            if to_create:
                PharmacyPublishable.objects.bulk_create(to_create, batch_size=1000)
            if to_refresh:
                PharmacyPublishable.objects.bulk_update(
                    to_refresh, fields=_COPY_FIELDS + ["status"], batch_size=1000)

        self.stdout.write(self.style.SUCCESS(
            f"Created {len(to_create)} new, refreshed {len(to_refresh)}, "
            f"left {skipped_edited} admin-edited untouched."))