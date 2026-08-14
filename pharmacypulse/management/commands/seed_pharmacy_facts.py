"""Seed the homepage's "Did you know" PharmacyFact table.

Idempotent — uses head as the natural key for upsert. Re-running won't create
duplicates and will refresh body/cta/src on existing rows. Pass --replace to
wipe the table and re-seed from scratch.

  python manage.py seed_pharmacy_facts          # upsert
  python manage.py seed_pharmacy_facts --replace # nuke + reseed
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from pharmacypulse.models import PharmacyFact

# Real, sourced retail-pharmacy facts. Keep `head` short (1 line) — that's
# the front of the card. `body` is the back. Sources are verifiable.
FACTS: list[dict] = [
    {
        "icon": "🏃", "stat": "13–14x / year",
        "head": "How often Americans visit a pharmacy",
        "body": "The average American visits a community pharmacy 13 to 14 times per year — more frequent than visits to any other healthcare provider.",
        "cta": "Does your pharmacy feel worth the visit?",
        "src": "NACDS",
    },
    {
        "icon": "🏠", "stat": "~90%",
        "head": "Of Americans live within 5 miles of a pharmacy",
        "body": "About 90% of Americans live within five miles of a community pharmacy, making it the most accessible point of care in the U.S. healthcare system.",
        "cta": "Is your nearest pharmacy actually convenient?",
        "src": "NACDS",
    },
    {
        "icon": "🏆", "stat": "#1",
        "head": "Most trusted profession in America",
        "body": "Pharmacists rank among the top three most trusted professions in Gallup's annual honesty and ethics poll, alongside nurses and medical doctors.",
        "cta": "Does your pharmacist earn that trust?",
        "src": "Gallup",
    },
    {
        "icon": "💉", "stat": "350M+",
        "head": "Vaccines administered by pharmacists",
        "body": "Pharmacists administered over 350 million vaccine doses during the COVID-19 era, becoming the leading provider of immunizations in the United States.",
        "cta": "Did your pharmacy make vaccines easy?",
        "src": "APhA",
    },
    {
        "icon": "💊", "stat": "~6.6B",
        "head": "Prescriptions filled annually in the U.S.",
        "body": "U.S. retail pharmacies fill an estimated 6.6 billion prescriptions every year — about 20 prescriptions per person.",
        "cta": "How smooth was your last refill?",
        "src": "IQVIA",
    },
    {
        "icon": "🧬", "stat": "~91%",
        "head": "Of prescriptions are filled with generics",
        "body": "Generic medications account for about 91% of prescriptions dispensed in the U.S., yet make up only ~18% of total drug spending.",
        "cta": "Did your pharmacy explain your generic options?",
        "src": "FDA",
    },
    {
        "icon": "⚠️", "stat": "~50%",
        "head": "Of patients don't take meds as prescribed",
        "body": "About half of patients with chronic conditions don't take their medications as prescribed — leading to ~125,000 preventable deaths annually.",
        "cta": "Does your pharmacist help you stay on track?",
        "src": "CDC",
    },
    {
        "icon": "📉", "stat": "300+",
        "head": "Active drug shortages in the U.S.",
        "body": "More than 300 active drug shortages were tracked nationwide in recent years — the highest in a decade. Local pharmacies are often the first to know.",
        "cta": "Have you been affected by a shortage?",
        "src": "ASHP",
    },
    {
        "icon": "🕐", "stat": "~10 min",
        "head": "Average pharmacy wait time",
        "body": "Patients wait an average of 8 to 12 minutes for a prescription pickup at a chain pharmacy — though wait times vary widely by location and time of day.",
        "cta": "How long was your last wait?",
        "src": "Patient Reports",
    },
    {
        "icon": "👥", "stat": "60K+",
        "head": "Independent pharmacies in the U.S.",
        "body": "There are over 19,000 independent community pharmacies in the U.S. plus 40,000+ chain locations — together serving as the primary medication touchpoint for most Americans.",
        "cta": "Local independent or national chain?",
        "src": "NCPA",
    },
    {
        "icon": "👨‍⚕️", "stat": "~330K",
        "head": "Licensed pharmacists in the U.S.",
        "body": "There are roughly 330,000 actively licensed pharmacists practicing in the U.S. — one of the largest healthcare professions in the country.",
        "cta": "Do you know your pharmacist by name?",
        "src": "BLS",
    },
    {
        "icon": "📱", "stat": "~75%",
        "head": "Of pharmacy customers use refill apps",
        "body": "About three in four pharmacy customers now use mobile apps or text alerts for prescription refills — but satisfaction with digital tools varies widely.",
        "cta": "Does your pharmacy's app actually work?",
        "src": "Drug Channels Institute",
    },
]


class Command(BaseCommand):
    help = "Seed homepage PharmacyFact entries (real retail-pharmacy stats with sources)."

    def add_arguments(self, parser):
        parser.add_argument("--replace", action="store_true",
                            help="Delete all existing facts before seeding")

    def handle(self, *args, **options):
        if options["replace"]:
            n = PharmacyFact.objects.count()
            PharmacyFact.objects.all().delete()
            self.stdout.write(self.style.WARNING(f"Deleted {n} existing facts"))

        created = updated = 0
        for i, f in enumerate(FACTS):
            obj, was_created = PharmacyFact.objects.update_or_create(
                head=f["head"],
                defaults={
                    "icon": f["icon"], "stat": f["stat"],
                    "body": f["body"], "cta": f["cta"],
                    "src": f["src"], "cta_link": f.get("cta_link", ""),
                    "sort_order": i, "is_active": True,
                },
            )
            if was_created:
                created += 1
            else:
                updated += 1

        self.stdout.write(self.style.SUCCESS(
            f"Done. {created} created, {updated} updated. "
            f"Active total: {PharmacyFact.objects.filter(is_active=True).count()}"
        ))
