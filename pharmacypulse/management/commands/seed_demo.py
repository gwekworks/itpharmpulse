"""
Port of BEN-156-pharmacypulse/seeds.sql adjusted for Django's auth model.

Seeds:
  - 3 demo accounts (patient, pharmacist, admin) with password "demo1234"
  - Moderation keywords (medication PHI list, profanity, legal threats)
  - 20 Philadelphia pharmacies with ratings/reviews pre-seeded
  - A handful of reviews + drug shortages

Usage:
  python manage.py seed_demo --reset
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from pharmacypulse.models import (
    DrugShortage, ModerationKeyword, Pharmacy, Review,
)

User = get_user_model()

DEMO_PASSWORD = "demo1234"

DEMO_USERS = [
    # (email, first, last, role, phone)
    ("patient@demo.pharmacypulse.com",    "Sarah",     "Mitchell", "user",       "2155550001"),
    ("pharmacist@demo.pharmacypulse.com", "Dr. Maria", "Santos",   "pharmacist", "2155550002"),
    ("admin@demo.pharmacypulse.com",      "Admin",     "User",     "admin",      "2155550003"),
]

MED_KEYWORDS = """
adderall ambien amoxicillin ativan atorvastatin azithromycin bupropion cialis
ciprofloxacin clonazepam clopidogrel codeine concerta cymbalta dexedrine
diazepam dilaudid doxycycline duloxetine escitalopram fentanyl fluoxetine
gabapentin hydrocodone ibuprofen insulin klonopin lexapro lisinopril lorazepam
losartan metformin methadone methotrexate methylphenidate metoprolol morphine
naproxen norco omeprazole ondansetron oxycodone oxycontin ozempic pantoprazole
percocet prednisone prozac ritalin semaglutide sertraline sildenafil suboxone
synthroid tamsulosin tramadol trazodone valium venlafaxine viagra vicodin
vyvanse warfarin wellbutrin xanax zoloft zolpidem mounjaro wegovy tirzepatide
trulicity jardiance eliquis xarelto humira enbrel keytruda entresto
diabetes cancer hiv hepatitis epilepsy schizophrenia bipolar adhd depression
ptsd pregnant chemotherapy dialysis herpes chlamydia gonorrhea
""".split()

PROFANITY_KEYWORDS = "fuck shit bitch damn bastard crap dick piss slut whore retard moron".split()

LEGAL_KEYWORDS = ["sue you", "my lawyer", "attorney", "lawsuit", "legal action",
                   "court", "i will sue", "press charges"]

PHARMACIES = [
    # (name, address, zip, phone, lat, lng, stock_conf, wait, rating, reviews, digital)
    ("Rittenhouse Pharmacy", "1800 Walnut St", "19103", "(215) 555-0100", 39.9496, -75.1724, 93, 7, 4.8, 28, 0),
    ("Center City Drug", "1234 Chestnut St", "19107", "(215) 555-0101", 39.9510, -75.1616, 87, 12, 4.3, 22, 0),
    ("Fishtown Health Rx", "2401 E Norris St", "19125", "(215) 555-0102", 39.9789, -75.1291, 78, 15, 3.9, 18, 0),
    ("South Philly Pharmacy", "1501 S Broad St", "19148", "(215) 555-0103", 39.9275, -75.1685, 95, 6, 4.8, 41, 0),
    ("Manayunk Medicine Shoppe", "4312 Main St", "19127", "(215) 555-0104", 40.0268, -75.2269, 83, 10, 4.1, 15, 0),
    ("University City Rx", "3800 Spruce St", "19104", "(215) 555-0105", 39.9501, -75.2024, 90, 9, 4.5, 28, 0),
    ("Old City Apothecary", "305 Market St", "19106", "(215) 555-0106", 39.9508, -75.1453, 74, 18, 3.7, 12, 0),
    ("Germantown Family Pharmacy", "5515 Germantown Ave", "19144", "(215) 555-0107", 40.0351, -75.1733, 88, 11, 4.4, 20, 0),
    ("Northern Liberties Drug", "903 N 2nd St", "19123", "(215) 555-0108", 39.9657, -75.1426, 81, 14, 4.0, 16, 0),
    ("Chestnut Hill Pharmacy", "8620 Germantown Ave", "19118", "(215) 555-0109", 40.0769, -75.2104, 96, 5, 4.9, 38, 0),
    ("West Philly Wellness Rx", "4700 Baltimore Ave", "19143", "(215) 555-0110", 39.9467, -75.2171, 72, 20, 3.6, 9, 0),
    ("Spring Garden Pharmacy", "1620 Spring Garden St", "19130", "(215) 555-0111", 39.9621, -75.1655, 85, 13, 4.2, 24, 0),
    ("Northeast Philly Rx", "7100 Frankford Ave", "19135", "(215) 555-0112", 40.0215, -75.0877, 79, 16, 3.8, 14, 0),
    ("Passyunk Square Pharmacy", "1340 E Passyunk Ave", "19147", "(215) 555-0113", 39.9360, -75.1565, 91, 7, 4.6, 31, 0),
    ("Kensington Corner Drug", "2200 E Allegheny Ave", "19134", "(215) 555-0114", 39.9874, -75.1192, 69, 22, 3.4, 8, 0),
    ("Society Hill Pharmacy", "400 S 4th St", "19147", "(215) 555-0115", 39.9444, -75.1478, 94, 6, 4.7, 33, 0),
    ("Point Breeze Pharmacy", "1801 Point Breeze Ave", "19145", "(215) 555-0116", 39.9247, -75.1823, 77, 17, 3.7, 13, 0),
    ("Mail Meds Online", "", "", "(800) 555-0200", None, None, 98, 0, 4.9, 412, 1),
    ("Capsule Digital Rx", "", "", "(800) 555-0201", None, None, 96, 0, 4.7, 289, 1),
    ("PillPack by Amazon", "", "", "(800) 555-0202", None, None, 95, 0, 4.6, 221, 1),
]

SHORTAGES = [
    # (drug_name, generic_name, manufacturer, status, reason)
    ("Ozempic",    "semaglutide",     "Novo Nordisk",   "current",  "Increased demand"),
    ("Adderall",   "amphetamine",     "Teva",           "current",  "Manufacturing delay"),
    ("Wegovy",     "semaglutide",     "Novo Nordisk",   "current",  "Increased demand"),
    ("Mounjaro",   "tirzepatide",     "Eli Lilly",      "current",  "Increased demand"),
    ("Amoxicillin","amoxicillin",     "Sandoz",         "resolved", ""),
]


class Command(BaseCommand):
    help = "Seed demo accounts, pharmacies, reviews, shortages, moderation keywords."

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true",
                            help="Wipe existing rows before seeding.")

    @transaction.atomic
    def handle(self, *args, reset: bool = False, **kwargs):
        if reset:
            self.stdout.write("Resetting existing data…")
            Review.objects.all().delete()
            Pharmacy.objects.all().delete()
            DrugShortage.objects.all().delete()
            ModerationKeyword.objects.all().delete()
            User.objects.filter(email__endswith="@demo.pharmacypulse.com").delete()

        # Users
        for email, fn, ln, role, phone in DEMO_USERS:
            u, created = User.objects.get_or_create(
                email=email,
                defaults={"username": email, "first_name": fn, "last_name": ln,
                           "role": role, "phone": phone},
            )
            if created:
                u.set_password(DEMO_PASSWORD); u.save()
                # Promote admin to Django staff + superuser for the django-admin panel
                if role == "admin":
                    u.is_staff = True; u.is_superuser = True; u.save()

        # Moderation keywords
        existing = set(ModerationKeyword.objects.values_list("keyword", "category"))
        def _bulk(keywords, category):
            ModerationKeyword.objects.bulk_create([
                ModerationKeyword(keyword=k, category=category)
                for k in keywords if (k, category) not in existing
            ])
        _bulk(MED_KEYWORDS, "medication")
        _bulk(PROFANITY_KEYWORDS, "profanity")
        _bulk(LEGAL_KEYWORDS, "legal")

        # Pharmacies
        for (name, address, zip_, phone, lat, lng, stock, wait, rating, n_reviews, digital) in PHARMACIES:
            Pharmacy.objects.get_or_create(
                name=name,
                defaults={
                    "address": address, "city": "Philadelphia" if not digital else "",
                    "state": "PA" if not digital else "", "zip": zip_,
                    "phone": phone, "latitude": lat, "longitude": lng,
                    "stock_confidence": stock, "avg_wait_time": wait,
                    "avg_service_rating": rating, "total_reviews": n_reviews,
                    "is_digital": digital, "status": "active",
                },
            )

        # Shortages
        for (drug, gen, manuf, status, reason) in SHORTAGES:
            DrugShortage.objects.get_or_create(
                drug_name=drug, manufacturer=manuf,
                defaults={"generic_name": gen, "status": status, "reason": reason},
            )

        # Reviews for retail pharmacies
        patient = User.objects.filter(email="patient@demo.pharmacypulse.com").first()
        if patient:
            top = list(Pharmacy.objects.filter(is_digital=0).order_by("-total_reviews")[:5])
            comments = [
                "Staff was incredibly helpful and friendly.",
                "Fast service, always has what I need in stock.",
                "Pharmacist took the time to explain everything.",
                "A bit of a wait but worth it.",
                "Convenient location, never any issues.",
            ]
            for p, c in zip(top, comments):
                if not Review.objects.filter(pharmacy=p, user=patient).exists():
                    Review.objects.create(
                        pharmacy=p, pharmacy_name=p.name,
                        user=patient, user_name=f"{patient.first_name} {patient.last_name}",
                        stock_available=1, wait_time_rating=5, service_rating=5,
                        comment=c,
                    )

        # Reviews for online pharmacies (so their detail pages aren't empty)
        # Disconnect rate-limit signal so seed can create multiple reviews per pharmacy
        from pharmacypulse.signals import review_pre_save
        from django.db.models.signals import pre_save
        pre_save.disconnect(review_pre_save, sender=Review)
        if patient:
            online_reviews = [
                ("Mail Meds Online",    5, 5, 1, "Incredibly fast shipping. Medications arrived in two days and were perfectly packaged."),
                ("Mail Meds Online",    4, 5, 1, "Super easy to transfer my prescription. Customer service was great when I had questions."),
                ("Mail Meds Online",    5, 5, 1, "Best prices I have found anywhere. Auto-refill saves me so much time every month."),
                ("Capsule Digital Rx",  5, 5, 1, "The app is so simple to use. Delivery is always on time and the pharmacist texts are genuinely helpful."),
                ("Capsule Digital Rx",  4, 5, 1, "Really impressed with how they handle insurance. Saved me a bunch compared to the local chain."),
                ("Capsule Digital Rx",  5, 4, 1, "Free same-day delivery in my area. Hard to beat that."),
                ("PillPack by Amazon",  5, 5, 1, "The pre-sorted daily packs are a game changer for managing multiple medications."),
                ("PillPack by Amazon",  4, 5, 1, "Seamless integration with Prime. Setup took five minutes and they handled the transfer."),
            ]
            for pharm_name, wait, service, stock, comment in online_reviews:
                p = Pharmacy.objects.filter(name=pharm_name).first()
                if p and not Review.objects.filter(pharmacy=p, comment=comment).exists():
                    Review.objects.create(
                        pharmacy=p, pharmacy_name=p.name,
                        user=patient, user_name=f"{patient.first_name} {patient.last_name}",
                        stock_available=stock, wait_time_rating=wait, service_rating=service,
                        comment=comment, delivery_timeliness=wait,
                    )
        pre_save.connect(review_pre_save, sender=Review)

        self.stdout.write(self.style.SUCCESS(
            f"Seeded: {User.objects.count()} users, "
            f"{Pharmacy.objects.count()} pharmacies, "
            f"{Review.objects.count()} reviews, "
            f"{DrugShortage.objects.count()} shortages, "
            f"{ModerationKeyword.objects.count()} moderation keywords."
        ))
        self.stdout.write("Demo accounts (password: demo1234):")
        for email, *_ in DEMO_USERS:
            self.stdout.write(f"  - {email}")
