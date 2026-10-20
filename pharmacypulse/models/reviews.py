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
    visit_purpose = models.TextField(blank=True, null=True)    # e.g. "refill,new_rx"
    visit_timeframe = models.TextField(blank=True, null=True)  # e.g. "yesterday"
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

    _PURPOSE_MAP = {
        "refill": {"label": "Refill pickup", "icon": "🔄"},
        "new_rx": {"label": "New prescription", "icon": "📋"},
        "transfer": {"label": "Transfer", "icon": "🔁"},
        "vaccine": {"label": "Vaccine", "icon": "💉"},
        "consultation": {"label": "Consultation", "icon": "💬"},
        "other": {"label": "Other / OTC", "icon": "•••"},
    }

    _TIMEFRAME_MAP = {
        "today": "Today",
        "yesterday": "Yesterday",
        "2-3_days": "2–3 days ago",
        "this_week": "Earlier this week",
        "last_week": "Last week",
        "this_month": "This month",
    }

    @property
    def visit_purpose_badges(self) -> list[dict[str, str]]:
        """Return list of formatted visit purpose badges (label + icon)."""
        if self.visit_purpose:
            items = []
            for code in self.visit_purpose.split(","):
                key = code.strip()
                if not key:
                    continue
                if key in self._PURPOSE_MAP:
                    items.append(self._PURPOSE_MAP[key])
                else:
                    items.append({"label": key.replace("_", " ").title(), "icon": "•"})
            if items:
                return items
        # Fallback for legacy reviews where visit_purpose is unset
        if self.stock_available:
            return [{"label": "Refill pickup", "icon": "🔄"}]
        return [{"label": "New prescription", "icon": "📋"}]

    @property
    def visit_timeframe_label(self) -> str | None:
        """Return human-readable visit timeframe label or None."""
        if not self.visit_timeframe:
            return None
        return self._TIMEFRAME_MAP.get(self.visit_timeframe, self.visit_timeframe)



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