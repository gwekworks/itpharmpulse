"""Review providers: reviews feed, write-review, for-pharmacies (biz reviews)."""
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
    BRAND_PATTERNS_BY_KEY, DEFUNCT_CHAIN_KEYS, published_pharmacies,
)


def page_reviews(request):
    from ..geo import resolve as resolve_loc
    approved = Review.objects.filter(
        deleted_at__isnull=True, moderation_status="approved",
    )
    # Localize to nearby pharmacies when we have a geo signal — patients
    # care more about reviews of pharmacies they could actually visit. Falls
    # back to global ordering when we have no signal.
    auto_loc = resolve_loc(request)
    pharm_filter = Q(status="active")
    review_pharm_filter = Q()
    if auto_loc.zip:
        zip3 = auto_loc.zip[:3]
        pharm_filter &= Q(zip__startswith=zip3)
        review_pharm_filter = Q(pharmacy__zip__startswith=zip3)

    nearby_approved = approved.filter(review_pharm_filter) if auto_loc.zip else approved
    # If localization yields too few results, widen back to global.
    if auto_loc.zip and nearby_approved.count() < 5:
        nearby_approved = approved

    qs = nearby_approved.select_related("pharmacy").order_by("-created_at")[:100]
    avg = approved.aggregate(v=Avg("service_rating"))["v"] or 0

    nearby_pharms = published_pharmacies().filter(pharm_filter)
    top_reviewed_qs = nearby_pharms.filter(total_reviews__gt=0).order_by("-total_reviews")[:6]
    if len(top_reviewed_qs) < 3:
        top_reviewed_qs = (published_pharmacies().filter(total_reviews__gt=0)
                           .order_by("-total_reviews")[:6])
    highest_rated_qs = nearby_pharms.filter(total_reviews__gte=3).order_by("-avg_service_rating")[:5]
    if len(highest_rated_qs) < 3:
        highest_rated_qs = (published_pharmacies().filter(total_reviews__gte=3)
                            .order_by("-avg_service_rating")[:5])

    return {
        "reviews": [_review_dict(r) for r in qs],
        # Stat tiles — kept global since "X pharmacies have been reviewed
        # nationwide" is the right scope for trust signaling.
        "r_total": [{"n": approved.count()}],
        "r_avg":   [{"avg": round(float(avg), 1)}],
        "r_pharm": [{"n": approved.exclude(pharmacy_id__isnull=True)
                       .values("pharmacy_id").distinct().count()}],
        # Sidebar lists — local-first, fall back to global if too thin.
        "top_reviewed":  [_pharmacy_dict(p) for p in top_reviewed_qs],
        "highest_rated": [_pharmacy_dict(p) for p in highest_rated_qs],
        # Main list — local-first.
        "all_reviews": [{**_review_dict(r),
                         "pharm_name": (r.pharmacy.name if r.pharmacy_id else "")}
                        for r in nearby_approved.select_related("pharmacy")
                        .order_by("-created_at")[:20]],
        "auto_loc_zip":    auto_loc.zip,
        "auto_loc_city":   auto_loc.city,
        "auto_loc_state":  auto_loc.state,
        "auto_loc_source": auto_loc.source,
    }


def page_write_review(request):
    pid = _safe_int(request.GET.get("pharmacy_id") or request.GET.get("id"))
    p = Pharmacy.objects.filter(id=pid).first() if pid else None
    # Template iterates `{% for item in rv_pharm %}` and `{% for item in rv_del %}`
    # then reads {{item.is_digital}}. Both prod aliases queried the same row.
    rv_rows = [{"is_digital": p.is_digital}] if p else []

    # Pre-check: if the user has already reviewed this pharmacy, show a
    # friendly "already reviewed" card instead of the form.
    last_review = None
    already_reviewed = False
    if pid and request.user.is_authenticated:
        last_review = (Review.objects
                       .filter(pharmacy_id=pid, user=request.user,
                               deleted_at__isnull=True)
                       .order_by("-created_at").first())
        if last_review:
            already_reviewed = True

    return {
        "pharmacy": Row(_pharmacy_dict(p)) if p else None,
        "pharmacy_id": p.id if p else "",
        "pharmacy_name": p.name if p else request.GET.get("pharmacy_name", ""),
        "pharmacy_url": (_pharmacy_dict(p)["url"] if p
                         else f"/pharmacy?id={pid if pid else request.GET.get('pharmacy_id', '')}"),
        "rv_pharm": rv_rows,
        "rv_del": rv_rows,
        "success": request.GET.get("success"),
        "error": request.GET.get("error"),
        # Override the param_error the decorator auto-injects so the banner
        # never displays a Python list literal repr like "['msg']".
        "param_error": _normalize_validation_error(request.GET.get("error") or ""),
        "already_reviewed": already_reviewed,
        "last_review_at": last_review.created_at if last_review else None,
    }


def page_for_pharmacies(request):
    approved = Review.objects.filter(deleted_at__isnull=True, moderation_status="approved")
    biz_reviews = []
    for r in approved.select_related("pharmacy", "user").order_by("-created_at")[:4]:
        biz_reviews.append({
            "id": r.id,
            "user_name": r.user_name or (r.user.first_name if r.user else "Patient"),
            "first_name": (r.user.first_name if r.user else None),
            "service_rating": r.service_rating,
            "comment": r.comment or "",
            "pharmacy_name": (r.pharmacy.name if r.pharmacy_id else (r.pharmacy_name or "")),
            "wait_time_rating": r.wait_time_rating,
            "created_at": r.created_at,
        })
    return {
        "active_pharmacies": Row({"total": Pharmacy.objects.filter(status="active").count()}),
        "total_reviews": Row({"total": approved.count()}),
        # Hero stat tiles read {{biz_pharm.0.n}} / {{biz_rev.0.n}} / {{biz_users.0.n}}.
        "biz_pharm":   [{"n": Pharmacy.objects.filter(status="active").count()}],
        "biz_rev":     [{"n": approved.count()}],
        "biz_users":   [{"n": User.objects.filter(deactivated_at__isnull=True).count()}],
        "biz_reviews": biz_reviews,
    }
