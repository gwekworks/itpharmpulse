"""Account + legal providers: page_account, privacy, terms, forgot-password."""
from __future__ import annotations

import functools
from datetime import timedelta

from django.core.cache import cache
from django.db.models import Avg, Case, Count, F, Q, Sum, Value, When
from django.utils import timezone as djtz

from ..models import (
    ActivityLog, DataRequest, DrugShortage, ModerationKeyword,
    NewsletterSubscriber, Notification, Pharmacy, PharmacyClaim, PharmacyHours,
    PharmacyOrg, PharmacyTeamMember, ResponseCount, Review, ReviewResponse,
    SavedComparison, User, UserConsent,
)

from .common import (
    Row, with_get_params, _safe_int, _pharmacy_dict, _review_dict,
    _shortage_dict, _compare_winners, _normalize_validation_error,
    _TAXONOMY_BADGE, _TAXONOMY_LABEL, _STATE_NAMES, BRAND_PATTERNS,
    BRAND_PATTERNS_BY_KEY, DEFUNCT_CHAIN_KEYS,
)


def page_account(request):
    u = request.user
    consents = list(UserConsent.objects.filter(user=u).order_by("consent_type"))
    my_reviews = []
    for r in (Review.objects.filter(user=u, deleted_at__isnull=True)
              .select_related("pharmacy").order_by("-created_at")):
        my_reviews.append({
            **_review_dict(r),
            "pharmacy_full_name": (r.pharmacy.name if r.pharmacy_id else (r.pharmacy_name or "")),
        })
    nl_status = list(NewsletterSubscriber.objects.filter(user=u)
                     .values("subscribed"))
    return {
        "me": Row({"id": u.id, "email": u.email, "first_name": u.first_name,
                   "last_name": u.last_name, "role": u.role, "phone": u.phone,
                   "zip_code": u.zip_code}),
        "consents": [{"type": c.consent_type, "granted": c.granted,
                      "granted_at": c.granted_at, "revoked_at": c.revoked_at}
                     for c in consents],
        "my_reviews": my_reviews,
        "nl_status": nl_status,
        "notifications": [{"id": n.id, "title": n.title, "body": n.body,
                           "read_at": n.read_at, "created_at": n.created_at}
                          for n in Notification.objects.filter(user=u)
                          .order_by("-created_at")[:30]],
        "activity": [{"id": a.id, "type": a.type, "message": a.message,
                      "created_at": a.created_at}
                     for a in ActivityLog.objects.filter(user=u)
                     .order_by("-created_at")[:50]],
    }


def page_privacy(request):
    return {}


def page_terms(request):
    return {}


def page_forgot_password(request):
    return {}
