"""Community/misc models: SavedComparison, NewsletterSubscriber, ModerationKeyword."""
from django.db import models


class SavedComparison(models.Model):
    user = models.ForeignKey(
        "User", on_delete=models.CASCADE, db_column="user_id",
        related_name="saved_comparisons",
    )
    pharmacy = models.ForeignKey(
        "Pharmacy", on_delete=models.CASCADE, db_column="pharmacy_id",
        related_name="saved_by",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "saved_comparisons"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "pharmacy"], name="uniq_saved_comparison_user_pharmacy"
            ),
        ]


class NewsletterSubscriber(models.Model):
    user = models.ForeignKey(
        "User", null=True, blank=True, on_delete=models.SET_NULL, db_column="user_id",
        related_name="newsletter",
    )
    email = models.TextField(unique=True)
    zip_code = models.TextField(blank=True, null=True)
    subscribed = models.IntegerField(default=1)
    subscribed_at = models.DateTimeField(auto_now_add=True)
    unsubscribed_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        db_table = "newsletter_subscribers"


class ModerationKeyword(models.Model):
    keyword = models.TextField()
    category = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "moderation_keywords"


class ClosedFlag(models.Model):
    pharmacy = models.ForeignKey(
        "Pharmacy", on_delete=models.CASCADE, db_column="pharmacy_id",
        related_name="closed_flags",
    )
    reporter_user = models.ForeignKey(
        "User", null=True, blank=True, on_delete=models.SET_NULL, db_column="reporter_user_id",
        related_name="pharmacy_closed_flags",
    )
    reporter_ip = models.TextField(blank=True, default="")
    note = models.TextField(blank=True, default="")
    status = models.TextField(default="pending")
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(blank=True, null=True)
    reviewed_by = models.ForeignKey(
        "User", null=True, blank=True, on_delete=models.SET_NULL, db_column="reviewed_by_id",
        related_name="reviewed_closed_flags",
    )

    class Meta:
        db_table = "closed_flags"