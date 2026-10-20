"""Pharmacy claim model."""
from django.db import models


class PharmacyClaim(models.Model):
    pharmacy = models.ForeignKey(
        "Pharmacy", null=True, blank=True, on_delete=models.SET_NULL, db_column="pharmacy_id",
        related_name="claims",
    )
    pharmacy_name = models.TextField(blank=True, null=True)
    user = models.ForeignKey(
        "User", null=True, blank=True, on_delete=models.SET_NULL, db_column="user_id",
        related_name="claims",
    )
    user_name = models.TextField(blank=True, null=True)
    npi_number = models.TextField()
    license_number = models.TextField()
    supporting_docs = models.TextField(blank=True, null=True)
    status = models.TextField(default="pending")
    reviewed_at = models.TextField(blank=True, null=True)
    reviewed_by = models.TextField(blank=True, null=True)
    rejection_reason = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "pharmacy_claims"