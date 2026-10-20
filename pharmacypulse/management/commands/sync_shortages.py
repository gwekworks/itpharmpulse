"""
Daily FDA drug shortage sync — mirrors the admin flow but runs headlessly.

Usage:
  python manage.py sync_shortages

Cron (daily at 6 AM server time):
  0 6 * * * cd /opt/benmore/apps/pharmacypulse && .venv/bin/python manage.py sync_shortages >> /var/log/pp_shortages.log 2>&1
"""
from __future__ import annotations

import requests
from django.core.management.base import BaseCommand

from pharmacypulse.models import DrugShortage


class Command(BaseCommand):
    help = "Sync current and resolved drug shortages from the FDA API"

    def handle(self, *args, **options):
        inserted = updated = 0
        for status_filter, stored in (("Current", "current"), ("Resolved", "resolved")):
            try:
                r = requests.get(
                    "https://api.fda.gov/drug/shortages.json",
                    params={
                        "search": f"status:{status_filter}",
                        "limit": 100 if stored == "current" else 50,
                    },
                    timeout=30,
                )
                if not r.ok:
                    self.stdout.write(self.style.WARNING(f"FDA API returned {r.status_code} for {status_filter}"))
                    continue
                for s in (r.json() or {}).get("results", []) or []:
                    name = s.get("generic_name") or ""
                    manuf = s.get("company_name") or ""
                    if not name:
                        continue
                    # FDA-supplied detail fields the previous sync was discarding:
                    #   related_info → reason (the human-readable shortage notes)
                    #   availability → alternatives (e.g. "Available", "Limited")
                    #   update_date  → estimated_resolution (closest signal we
                    #                  have without a schema change)
                    related_info = (s.get("related_info") or "").strip()
                    availability = (s.get("availability") or "").strip()
                    update_date = (s.get("update_date") or "").strip()
                    obj, created = DrugShortage.objects.update_or_create(
                        drug_name=name, manufacturer=manuf,
                        defaults={
                            "generic_name": name,
                            "status": stored,
                            "reason": related_info[:500],
                            "alternatives": availability[:500],
                            "estimated_resolution": update_date,
                        },
                    )
                    if created:
                        inserted += 1
                    else:
                        updated += 1
            except requests.RequestException as exc:
                self.stdout.write(self.style.WARNING(f"Request error for {status_filter}: {exc}"))
                continue

        self.stdout.write(self.style.SUCCESS(
            f"Shortage sync complete — {inserted} new, {updated} updated."
        ))
