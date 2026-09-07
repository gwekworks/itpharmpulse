"""
Hooks → Django signals. Ports BEN-156-pharmacypulse/hooks.yaml 1:1.

- reviews insert: PHI/profanity moderation (pre_save rate limit), recompute
  pharmacy stats with 7/14/30-day time decay for stock_confidence.
- reviews update: recompute stats; if response_text added, increment the
  per-pharmacy monthly response_counts row.
- reviews delete: activity_log.
- pharmacy_claims insert/update: activity_log.
- _benmore_users insert: TCPA/ToS/DNS consents.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Avg, Count, F, Sum
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone as djtz

from .models import (
    ActivityLog, ModerationKeyword, Pharmacy, PharmacyClaim,
    ResponseCount, Review, User, UserConsent,
)

# ---------------- Review: rate limit + moderation (pre_save) ----------------

RATE_LIMIT_WINDOW = timedelta(days=1)


@receiver(pre_save, sender=Review)
def review_pre_save(sender, instance: Review, **kwargs):
    # Only enforce on insert (new reviews). Update path skips these gates.
    if instance.pk is not None:
        return

    # Rate limit: 1 review per pharmacy per user per 24h
    if instance.user_id and instance.pharmacy_id:
        cutoff = djtz.now() - RATE_LIMIT_WINDOW
        if Review.objects.filter(
            pharmacy_id=instance.pharmacy_id,
            user_id=instance.user_id,
            created_at__gt=cutoff,
            deleted_at__isnull=True,
        ).exists():
            raise ValidationError(
                "You can only submit one review per pharmacy every 24 hours"
            )

    comment = (instance.comment or "").lower()
    if comment:
        # PHI (medication names)
        med_hits = ModerationKeyword.objects.filter(category="medication")
        if any(kw.keyword.lower() in comment for kw in med_hits):
            raise ValidationError(
                "Your review may contain medication names or personal health "
                "information. Please remove specific medication names for your privacy."
            )
        # Profanity
        prof_hits = ModerationKeyword.objects.filter(category="profanity")
        if any(kw.keyword.lower() in comment for kw in prof_hits):
            raise ValidationError(
                "Your review contains inappropriate language. Please revise and resubmit."
            )


# ------------ Review: recompute pharmacy stats (insert + update) -----------

def _recompute_pharmacy_stats(pharmacy_id: int):
    """Replicates the SQL in hooks.yaml on_insert/on_update reviews."""
    qs = Review.objects.filter(pharmacy_id=pharmacy_id, deleted_at__isnull=True)
    total = qs.count()
    agg = qs.aggregate(
        avg_service=Avg("service_rating"), avg_wait=Avg("wait_time_rating")
    )

    # Time-decay stock confidence: 0-7d weight 1.0, 7-14d 0.5, 14-30d 0.25, else 0
    now = djtz.now()
    recent = qs.filter(created_at__gt=now - timedelta(days=30))
    num = 0.0
    den = 0.0
    for r in recent.only("created_at", "stock_available"):
        delta = (now - r.created_at).total_seconds() / 86400.0
        if delta <= 7:
            w = 1.0
        elif delta <= 14:
            w = 0.5
        else:
            w = 0.25
        # stock_available: 0=out, 1=in stock, 2=partially in stock (counts as 0.5)
        sv = r.stock_available or 0
        contrib = 1.0 if sv == 1 else (0.5 if sv == 2 else 0.0)
        num += contrib * w
        den += w
    stock_pct = round(num / den * 100) if den > 0 else 0

    Pharmacy.objects.filter(id=pharmacy_id).update(
        total_reviews=total,
        avg_service_rating=round((agg["avg_service"] or 0), 1),
        avg_wait_time=round(agg["avg_wait"] or 0),
        stock_confidence=stock_pct,
        updated_at=now,
    )


@receiver(post_save, sender=Review)
def review_post_save(sender, instance: Review, created: bool, **kwargs):
    if instance.pharmacy_id:
        transaction.on_commit(lambda: _recompute_pharmacy_stats(instance.pharmacy_id))
    if created:
        ActivityLog.objects.create(
            type="review_created",
            message=f"New review for {instance.pharmacy_name or ''}".strip(),
            user_id=instance.user_id,
        )
        return
    # Update path: if response_text is present, tick response_counts for this
    # pharmacy's current YYYY-MM bucket. Only ticks once per save — rate-limit
    # the free-tier counter is enforced in the flow, not here.
    if instance.response_text and instance.response_text.strip():
        month = djtz.now().strftime("%Y-%m")
        row, was_new = ResponseCount.objects.get_or_create(
            pharmacy_id=instance.pharmacy_id, month=month, defaults={"count": 1}
        )
        if not was_new:
            ResponseCount.objects.filter(pk=row.pk).update(count=F("count") + 1)


@receiver(post_delete, sender=Review)
def review_post_delete(sender, instance: Review, **kwargs):
    ActivityLog.objects.create(
        type="review_deleted",
        message="Review deleted",
        user_id=instance.user_id,
    )


# ------------------ PharmacyClaim: activity log on insert / update ---------

@receiver(post_save, sender=PharmacyClaim)
def claim_post_save(sender, instance: PharmacyClaim, created: bool, **kwargs):
    if created:
        ActivityLog.objects.create(
            type="claim_submitted",
            message=f"Claim submitted for {instance.pharmacy_name or ''}".strip(),
            user_id=instance.user_id,
        )
    elif instance.status != "pending":
        ActivityLog.objects.create(
            type="claim_updated",
            message=(
                f"Claim for {instance.pharmacy_name or ''} updated to {instance.status}"
            ).strip(),
            user_id=instance.user_id,
        )


# -------------------------- User: default consents on signup ---------------

@receiver(post_save, sender=User)
def user_post_save(sender, instance: User, created: bool, **kwargs):
    if not created:
        return
    now = djtz.now()
    defaults = [
        ("tcpa_sms", 1),
        ("terms_of_service", 1),
        ("do_not_sell", 1),
    ]
    UserConsent.objects.bulk_create([
        UserConsent(user=instance, consent_type=ct, granted=g, granted_at=now)
        for ct, g in defaults
    ])
