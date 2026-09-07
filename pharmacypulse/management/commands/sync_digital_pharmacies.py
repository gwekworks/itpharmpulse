"""Seed/upsert well-known US digital (online-only) pharmacies.

Google Places nearbysearch can't surface online-only pharmacies because they
have no physical storefront, so the nationwide retail sync misses them
entirely. This command upserts a curated list of major US consumer-facing
online pharmacies with is_digital=1.

Idempotent: uses npi_number='DIGITAL_<slug>' as the natural key so re-runs
update existing rows instead of duplicating. Pass --replace to nuke and
reseed all is_digital=1 rows.

  python manage.py sync_digital_pharmacies          # upsert
  python manage.py sync_digital_pharmacies --replace # wipe + reseed

NABP accreditation status is the pharmacy's own public claim — verify
against NABP's accredited registry before relying on it for any
compliance-sensitive use case.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from pharmacypulse.models import Pharmacy

# (slug, name, website, phone, nabp_accredited, blurb)
DIGITAL_PHARMACIES: list[dict] = [
    # Big-retail mail-order arms
    {"slug": "cvs-online", "name": "CVS Pharmacy (Online)", "website": "https://www.cvs.com/pharmacy", "phone": "1-888-607-4287", "nabp": True},
    {"slug": "walgreens-online", "name": "Walgreens (Online)", "website": "https://www.walgreens.com/pharmacy", "phone": "1-800-925-4733", "nabp": True},
    {"slug": "walmart-pharmacy", "name": "Walmart Pharmacy", "website": "https://www.walmart.com/cp/pharmacy/5431", "phone": "1-800-925-6278", "nabp": True},
    {"slug": "costco-pharmacy", "name": "Costco Pharmacy", "website": "https://www.costco.com/pharmacy.html", "phone": "1-800-607-6861", "nabp": True},
    {"slug": "sams-club-pharmacy", "name": "Sam's Club Pharmacy", "website": "https://www.samsclub.com/c/pharmacy", "phone": "1-866-237-1955", "nabp": True},
    {"slug": "rite-aid-online", "name": "Rite Aid Online Pharmacy", "website": "https://www.riteaid.com/pharmacy", "phone": "1-800-748-3243", "nabp": True},
    {"slug": "kroger-health-pharmacy", "name": "Kroger Health Pharmacy", "website": "https://www.kroger.com/rx", "phone": "1-855-489-2502", "nabp": True},

    # Amazon family
    {"slug": "amazon-pharmacy", "name": "Amazon Pharmacy", "website": "https://pharmacy.amazon.com", "phone": "1-855-745-5725", "nabp": True},
    {"slug": "pillpack", "name": "PillPack by Amazon", "website": "https://www.pillpack.com", "phone": "1-855-745-5725", "nabp": True},

    # Direct-to-consumer pure-plays
    {"slug": "cost-plus-drugs", "name": "Cost Plus Drug Company", "website": "https://costplusdrugs.com", "phone": "1-833-926-3384", "nabp": True},
    {"slug": "capsule", "name": "Capsule", "website": "https://www.capsule.com", "phone": "1-855-227-8753", "nabp": True},
    {"slug": "alto-pharmacy", "name": "Alto Pharmacy", "website": "https://alto.com", "phone": "1-800-874-5881", "nabp": True},
    {"slug": "nowrx", "name": "NowRx Pharmacy", "website": "https://www.nowrx.com", "phone": "1-855-668-9979", "nabp": True},
    {"slug": "honeybee-health", "name": "Honeybee Health", "website": "https://honeybeehealth.com", "phone": "1-833-466-3979", "nabp": True},
    {"slug": "blink-health", "name": "Blink Health Pharmacy", "website": "https://www.blinkhealth.com", "phone": "1-844-265-6444", "nabp": True},
    {"slug": "scriptco", "name": "ScriptCo Pharmacy", "website": "https://scriptco.com", "phone": "1-800-988-4651", "nabp": True},
    {"slug": "goodrx-pharmacy", "name": "GoodRx Care Pharmacy", "website": "https://www.goodrx.com/care/pharmacy", "phone": "1-855-268-2822", "nabp": False},
    {"slug": "truepill", "name": "Truepill Pharmacy", "website": "https://www.truepill.com", "phone": "1-855-748-9088", "nabp": True},

    # Telehealth-attached pharmacies
    {"slug": "hims-hers", "name": "Hims & Hers Pharmacy", "website": "https://www.hims.com", "phone": "1-800-368-0038", "nabp": False},
    {"slug": "ro-pharmacy", "name": "Ro Pharmacy", "website": "https://ro.co", "phone": "1-888-798-0455", "nabp": True},
    {"slug": "lemonaid-pharmacy", "name": "Lemonaid Health Pharmacy", "website": "https://www.lemonaidhealth.com", "phone": "1-415-484-9300", "nabp": False},
    {"slug": "wisp", "name": "Wisp Pharmacy", "website": "https://hellowisp.com", "phone": "1-844-947-7937", "nabp": False},

    # PBM / mail-service pharmacies (high prescription volume nationally)
    {"slug": "express-scripts", "name": "Express Scripts Pharmacy", "website": "https://www.express-scripts.com", "phone": "1-800-282-2881", "nabp": True},
    {"slug": "cvs-caremark", "name": "CVS Caremark Mail Service", "website": "https://www.caremark.com", "phone": "1-800-552-8159", "nabp": True},
    {"slug": "optumrx", "name": "OptumRx Home Delivery", "website": "https://www.optumrx.com", "phone": "1-800-356-3477", "nabp": True},
    {"slug": "humana-pharmacy", "name": "Humana Pharmacy", "website": "https://www.humanapharmacy.com", "phone": "1-800-379-0092", "nabp": True},

    # Specialty / niche
    {"slug": "accredo", "name": "Accredo Specialty Pharmacy", "website": "https://www.accredo.com", "phone": "1-800-803-2523", "nabp": True},
    {"slug": "cvs-specialty", "name": "CVS Specialty", "website": "https://www.cvsspecialty.com", "phone": "1-800-237-2767", "nabp": True},
    {"slug": "rexmd", "name": "RexMD Pharmacy", "website": "https://www.rexmd.com", "phone": "1-866-687-8631", "nabp": False},
    {"slug": "valisure", "name": "Valisure Pharmacy", "website": "https://www.valisure.com", "phone": "1-203-680-1107", "nabp": False},
]


class Command(BaseCommand):
    help = "Seed/upsert curated US digital (online-only) pharmacies."

    def add_arguments(self, parser):
        parser.add_argument("--replace", action="store_true",
                            help="Delete all existing is_digital=1 pharmacies first")

    def handle(self, *args, **options):
        if options["replace"]:
            n = Pharmacy.objects.filter(is_digital=1).count()
            Pharmacy.objects.filter(is_digital=1).delete()
            self.stdout.write(self.style.WARNING(f"Deleted {n} existing digital pharmacies"))

        created = updated = 0
        for entry in DIGITAL_PHARMACIES:
            npi = f"DIGITAL_{entry['slug']}"
            obj, was_created = Pharmacy.objects.update_or_create(
                npi_number=npi,
                defaults={
                    "name": entry["name"],
                    "website": entry["website"],
                    "phone": entry.get("phone", ""),
                    "is_digital": 1,
                    "status": "active",
                    # No physical address — these are mail-order/online-only.
                    # latitude/longitude stay NULL so the map filter (is_digital=0)
                    # excludes them; the /online listing page picks them up.
                    "address": "Online pharmacy",
                    "city": "",
                    "state": "",
                    "zip": "",
                    "latitude": None,
                    "longitude": None,
                },
            )
            if was_created:
                created += 1
            else:
                updated += 1

        total = Pharmacy.objects.filter(is_digital=1, status="active").count()
        self.stdout.write(self.style.SUCCESS(
            f"Done. {created} created, {updated} updated. "
            f"Total active digital pharmacies: {total}"
        ))
