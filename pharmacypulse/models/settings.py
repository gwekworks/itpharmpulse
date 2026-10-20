"""Platform settings stored in the DB, editable from the admin dashboard."""
from django.db import models


class SiteSetting(models.Model):
    """Simple key/value store for platform-wide settings admins tune at
    runtime without a redeploy (e.g. the pharmacy search radius)."""
    key = models.CharField(max_length=100, unique=True)
    value = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "site_settings"

    def __str__(self):
        return f"{self.key}={self.value}"
