"""Review models: Review, ReviewResponse, ResponseCount."""
from django.db import models


class Review(models.Model):
    pharmacy = models.ForeignKey(
        "Pharmacy", null=True, blank=True, on_delete=models.SET_NULL, db_column="pharmacy_id",
        related_name="reviews",
    )
    pharmacy_name = models.TextField(blank=True, null=True)
    user = models.ForeignKey(
        "User", null=True, blank=True, on_delete=models.SET_NULL, db_column="user_id",
        related_name="reviews",
    )
    user_name = models.TextField(blank=True, null=True)
    stock_available = models.IntegerField(default=1)
    wait_time_rating = models.IntegerField(default=3)
    service_rating = models.IntegerField(default=3)
    comment = models.TextField(blank=True, null=True)
    delivery_timeliness = models.IntegerField(blank=True, null=True)
    is_caregiver = models.IntegerField(default=0)
    response_text = models.TextField(blank=True, null=True)
    response_date = models.TextField(blank=True, null=True)
    moderation_status = models.TextField(default="approved")
    flag_reason = models.TextField(blank=True, null=True)
    flagged_at = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        db_table = "reviews"
        constraints = [
            models.UniqueConstraint(
                fields=["pharmacy", "user"],
                name="uniq_review_pharmacy_user",
                condition=models.Q(deleted_at__isnull=True),
            ),
        ]


class ReviewResponse(models.Model):
    """Track which user responded to which review."""
    review = models.ForeignKey(
        "Review", on_delete=models.CASCADE, db_column="review_id",
        related_name="responses",
    )
    responder = models.ForeignKey(
        "User", on_delete=models.CASCADE, db_column="responder_id",
        related_name="review_responses",
    )
    responder_name = models.TextField(blank=True, null=True)
    org = models.ForeignKey(
        "PharmacyOrg", null=True, blank=True, on_delete=models.SET_NULL, db_column="org_id",
        related_name="review_responses",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "review_responses"


class ResponseCount(models.Model):
    pharmacy = models.ForeignKey(
        "Pharmacy", null=True, blank=True, on_delete=models.CASCADE, db_column="pharmacy_id",
        related_name="response_counts",
    )
    month = models.TextField()  # 'YYYY-MM'
    count = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "response_counts"
        constraints = [
            models.UniqueConstraint(
                fields=["pharmacy", "month"], name="uniq_response_counts_pharmacy_month"
            ),
        ]