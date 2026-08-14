"""Drug shortage models: DrugShortage + PinnedShortage."""
from django.db import models


class DrugShortage(models.Model):
    drug_name = models.TextField()
    generic_name = models.TextField(blank=True, null=True)
    manufacturer = models.TextField(blank=True, null=True)
    status = models.TextField(default="current")
    reason = models.TextField(blank=True, null=True)
    estimated_resolution = models.TextField(blank=True, null=True)
    alternatives = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "drug_shortages"
        constraints = [
            models.UniqueConstraint(
                fields=["drug_name", "manufacturer"], name="uniq_shortage_drug_manuf"
            ),
        ]


class PinnedShortage(models.Model):
    user = models.ForeignKey(
        "User", null=True, blank=True, on_delete=models.CASCADE, db_column="user_id",
        related_name="pinned_shortages",
    )
    shortage = models.ForeignKey(
        "DrugShortage", null=True, blank=True, on_delete=models.CASCADE, db_column="shortage_id",
        related_name="pinned_by",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "pinned_shortages"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "shortage"], name="uniq_pinned_user_shortage"
            ),
        ]