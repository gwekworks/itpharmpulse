"""Homepage 'Did you know' fact cards."""
from django.db import models


class PharmacyFact(models.Model):
    """Editable "Did you know" cards on the homepage. Apr-16 sync action item:
    admin tool to edit facts content (text + links) without re-deploying."""
    icon = models.TextField(blank=True, default="")
    stat = models.TextField(blank=True, default="")
    head = models.TextField()
    body = models.TextField()
    cta = models.TextField(blank=True, default="")
    src = models.TextField(blank=True, default="")
    cta_link = models.TextField(blank=True, default="")
    sort_order = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "pharmacy_facts"
        ordering = ["sort_order", "id"]