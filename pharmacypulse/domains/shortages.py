"""Drug-shortage provider: page_shortages."""
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


def page_shortages(request):
    status = (request.GET.get("status") or "current").strip()
    q = (request.GET.get("q") or "").strip()
    qs = DrugShortage.objects.filter(status=status)
    if q:
        qs = qs.filter(Q(drug_name__icontains=q) | Q(generic_name__icontains=q))
    qs = qs.order_by("-updated_at")
    pinned = set()
    if request.user.is_authenticated:
        from ..models import PinnedShortage
        pinned = set(PinnedShortage.objects.filter(user=request.user)
                     .values_list("shortage_id", flat=True))
    all_shortages = DrugShortage.objects.all()
    return {
        "shortages": [{**_shortage_dict(s), "pinned": s.id in pinned} for s in qs[:500]],
        "total_count": Row({"total": qs.count()}),
        "current_status": status,
        # Tile-stat aliases the template reads as {{s_total.0.n}} etc.
        "s_total":    [{"n": all_shortages.count()}],
        "s_current":  [{"n": all_shortages.filter(status="current").count()}],
        "s_resolved": [{"n": all_shortages.filter(status="resolved").count()}],
    }
