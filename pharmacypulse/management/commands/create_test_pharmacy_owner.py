"""Create one test pharmacy and one test pharmacy-owner user. Idempotent.

Useful for poking at the pharmacist-dashboard, claim flow, and review-response
flow without going through real NPI verification.

Usage:
  python manage.py create_test_pharmacy_owner
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from pharmacypulse.models import (
    Pharmacy, PharmacyClaim, PharmacyOrg, PharmacyTeamMember, Review,
)

User = get_user_model()

TEST_EMAIL = "owner@testpharmacy.pharmacypulse.com"
TEST_PASSWORD = "testowner1234"
TEST_PATIENT_EMAIL = "patient@testpharmacy.pharmacypulse.com"
TEST_PATIENT_PASSWORD = "testpatient1234"
TEST_PHARMACY_NAME = "Test Pharmacy (Demo)"

SEED_REVIEWS = [
    # (stock, wait_rating, service_rating, comment)
    (1, 5, 5, "Friendly staff, prescription was ready when I arrived."),
    (1, 4, 5, "The pharmacist took the time to walk me through a new medication."),
    (1, 5, 4, "Quick service, never a long line on weekday mornings."),
    (0, 3, 4, "Was out of stock once but they called around to a sister location for me."),
]


class Command(BaseCommand):
    help = "Create a test pharmacy + pharmacy-owner user (idempotent)."

    @transaction.atomic
    def handle(self, *args, **opts):
        # Pharmacy aggregate fields (stock_confidence, avg_*) are recomputed
        # from real reviews by the review_post_save signal — leave them at
        # zero on creation rather than baking in fake numbers.
        pharmacy, p_created = Pharmacy.objects.get_or_create(
            name=TEST_PHARMACY_NAME,
            defaults={
                "address": "1234 Demo Street",
                "city": "Philadelphia",
                "state": "PA",
                "zip": "19103",
                "phone": "215-555-0100",
                "latitude": 39.9526,
                "longitude": -75.1652,
                "is_digital": 0,
                "status": "active",
                "npi_number": "1234567893",
            },
        )

        user, u_created = User.objects.get_or_create(
            email=TEST_EMAIL,
            defaults={
                "username": TEST_EMAIL,
                "first_name": "Test",
                "last_name": "Owner",
                "role": "pharmacist",
                "phone": "2155550100",
                "zip_code": "19103",
            },
        )
        if u_created:
            user.set_password(TEST_PASSWORD)
            user.save()
        elif user.role != "pharmacist":
            user.role = "pharmacist"
            user.save(update_fields=["role"])

        org, _ = PharmacyOrg.objects.get_or_create(
            name=TEST_PHARMACY_NAME, owner=user,
        )

        Pharmacy.objects.filter(id=pharmacy.id).update(
            claimed_by=user.id, org_id=org.id,
        )

        PharmacyTeamMember.objects.get_or_create(
            org=org, user=user,
            defaults={"email": user.email, "role": "owner",
                      "accepted_at": timezone.now()},
        )

        PharmacyClaim.objects.get_or_create(
            pharmacy_id=pharmacy.id, user=user,
            defaults={
                "pharmacy_name": pharmacy.name,
                "user_name": "Test Owner",
                "npi_number": "1234567893",
                "license_number": "TESTPA-001",
                "status": "approved",
                "reviewed_at": timezone.now().isoformat(),
                "reviewed_by": "Test seed",
            },
        )

        patient, pa_created = User.objects.get_or_create(
            email=TEST_PATIENT_EMAIL,
            defaults={
                "username": TEST_PATIENT_EMAIL,
                "first_name": "Test",
                "last_name": "Patient",
                "role": "user",
                "phone": "2155550101",
                "zip_code": "19103",
            },
        )
        if pa_created:
            patient.set_password(TEST_PATIENT_PASSWORD)
            patient.save()

        # Bypass the rate-limit pre_save signal so the seed can drop multiple
        # reviews from the same user. The post_save signal stays connected so
        # pharmacy aggregate stats recompute as each review lands.
        from django.db.models.signals import pre_save
        from pharmacypulse.signals import review_pre_save
        pre_save.disconnect(review_pre_save, sender=Review)
        try:
            for stock, wait_r, service_r, comment in SEED_REVIEWS:
                if Review.objects.filter(pharmacy=pharmacy, comment=comment).exists():
                    continue
                Review.objects.create(
                    pharmacy=pharmacy, pharmacy_name=pharmacy.name,
                    user=patient,
                    user_name=f"{patient.first_name} {patient.last_name}",
                    stock_available=stock,
                    wait_time_rating=wait_r,
                    service_rating=service_r,
                    comment=comment,
                )
        finally:
            pre_save.connect(review_pre_save, sender=Review)

        pharmacy.refresh_from_db()
        self.stdout.write(self.style.SUCCESS(
            "\n=== Test pharmacy + owner ready ===\n"
            f"Pharmacy:  {pharmacy.name} (id={pharmacy.id})  {'[created]' if p_created else '[existed]'}\n"
            f"Reviews:   {pharmacy.total_reviews}  ·  rating {pharmacy.avg_service_rating}  ·  stock {pharmacy.stock_confidence}%\n"
            f"\nOwner login\n"
            f"  Email:    {TEST_EMAIL}\n"
            f"  Password: {TEST_PASSWORD}\n"
            f"  Role:     pharmacist  {'[created]' if u_created else '[existed]'}\n"
            f"\nPatient login (for leaving reviews)\n"
            f"  Email:    {TEST_PATIENT_EMAIL}\n"
            f"  Password: {TEST_PATIENT_PASSWORD}  {'[created]' if pa_created else '[existed]'}\n"
            f"\nDashboard: /pharmacist-dashboard\n"
            f"Public:    /pharmacy?id={pharmacy.id}\n"
        ))
