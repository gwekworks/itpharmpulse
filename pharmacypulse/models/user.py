"""User + auth-adjacent models (consents, notifications, activity, data requests)."""
from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """Replaces _benmore_users. Email is the identifier; username kept
    auto-derived for Django admin compatibility."""
    ROLE_CHOICES = (
        ("admin", "Administrator"),
        ("pharmacist", "Pharmacist"),
        ("user", "User"),
    )
    email = models.EmailField(unique=True)
    role = models.CharField(max_length=16, default="user", choices=ROLE_CHOICES)
    phone = models.CharField(max_length=32, blank=True, default="")
    zip_code = models.CharField(max_length=16, blank=True, default="")
    google_sub = models.CharField(max_length=64, blank=True, default="", db_index=True)
    facebook_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    deactivated_at = models.DateTimeField(blank=True, null=True)

    # Stripe subscription state — populated by the /api/flow/stripe-webhook
    # handler. plan='pro' gates the unlimited-responses + advanced-analytics
    # features. status mirrors Stripe's subscription.status (active, past_due,
    # canceled, etc.). period_end is when the current paid window ends — used
    # to compute "expires on" copy and to gate access after cancellation.
    stripe_customer_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    stripe_subscription_id = models.CharField(max_length=64, blank=True, default="")
    plan = models.CharField(max_length=16, default="free")  # 'free' | 'pro'
    subscription_status = models.CharField(max_length=32, blank=True, default="")
    subscription_period_end = models.DateTimeField(blank=True, null=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["username"]

    class Meta:
        db_table = "_benmore_users"


class Notification(models.Model):
    """Mirrors Benmore's _benmore_notifications table."""
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, db_column="user_id", related_name="notifications"
    )
    title = models.TextField()
    body = models.TextField(blank=True, default="")
    read_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "_benmore_notifications"


class ActivityLog(models.Model):
    type = models.TextField()
    message = models.TextField(blank=True, null=True)
    user = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, db_column="user_id",
        related_name="activity_events",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "activity_log"


class DataRequest(models.Model):
    user = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, db_column="user_id",
        related_name="data_requests",
    )
    request_type = models.TextField()
    status = models.TextField(default="pending")
    requested_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(blank=True, null=True)
    due_date = models.DateTimeField(blank=True, null=True)

    class Meta:
        db_table = "data_requests"


class UserConsent(models.Model):
    user = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.CASCADE, db_column="user_id",
        related_name="consents",
    )
    consent_type = models.TextField()
    granted = models.IntegerField(default=0)
    granted_at = models.DateTimeField(blank=True, null=True)
    revoked_at = models.DateTimeField(blank=True, null=True)
    ip_address = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "user_consents"
