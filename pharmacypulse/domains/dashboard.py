"""Pharmacist + admin dashboard providers."""
from __future__ import annotations

import functools
from datetime import timedelta

from django.core.cache import cache
from django.db.models import Avg, Case, Count, F, Q, Sum, Value, When
from django.utils import timezone as djtz

from ..models import (
    ActivityLog, ClosedFlag, DataRequest, DrugShortage, ModerationKeyword,
    NewsletterSubscriber, Notification, Pharmacy, PharmacyClaim,
    PharmacyCoveragePlan, PharmacyHours, PharmacyInsuranceProvider, PharmacyOrg,
    PharmacyPublishable, PharmacyTeamMember, ResponseCount, Review,
    ReviewResponse, SavedComparison, User, UserConsent,
)

from .common import (
    Row, with_get_params, _safe_int, _pharmacy_dict, _review_dict,
    _shortage_dict, _compare_winners, _normalize_validation_error,
    _TAXONOMY_BADGE, _TAXONOMY_LABEL, _STATE_NAMES, BRAND_PATTERNS,
    BRAND_PATTERNS_BY_KEY, DEFUNCT_CHAIN_KEYS, get_search_radius_mi,
)


@with_get_params
def page_pharmacist_dashboard(request):
    u = request.user
    tab = request.GET.get("tab", "reviews")

    # Resolve the set of pharmacies this user controls.
    # Prod SQL `pharmacy_claims WHERE user_id = X AND status='approved' UNION
    # team members WHERE user_id = X AND accepted_at IS NOT NULL`.
    claimed_ids = set(PharmacyClaim.objects.filter(user=u, status="approved")
                      .values_list("pharmacy_id", flat=True))
    my_tm = (PharmacyTeamMember.objects.filter(user=u, accepted_at__isnull=False)
             .select_related("org").first())
    org = my_tm.org if my_tm else None
    team_pharm_ids = set(Pharmacy.objects.filter(org=org).values_list("id", flat=True)) if org else set()
    my_pharm_ids = claimed_ids | team_pharm_ids

    my_pharms_qs = Pharmacy.objects.filter(id__in=my_pharm_ids)
    primary_pharm = my_pharms_qs.first()  # template's `my_pharmacy` is at-most-one
    primary_pid = primary_pharm.id if primary_pharm else None

    claimed_pharms_qs = Pharmacy.objects.filter(id__in=claimed_ids)
    claimed_reviews = Review.objects.filter(pharmacy__in=claimed_pharms_qs,
                                            deleted_at__isnull=True)
    approved_claimed = claimed_reviews.filter(moderation_status="approved")
    recent_reviews = list(claimed_reviews.order_by("-created_at")[:50])

    month = djtz.now().strftime("%Y-%m")
    responses_used = (ResponseCount.objects.filter(
        pharmacy__in=my_pharms_qs, month=month).aggregate(t=Sum("count"))["t"] or 0)

    # Aggregate stat blocks (each rendered with {% for item in alias %} so a 1-elem list works)
    resp_total = approved_claimed.count()
    resp_responded = approved_claimed.exclude(response_text="").exclude(response_text__isnull=True).count()

    cutoff_7  = djtz.now() - timedelta(days=7)
    cutoff_30 = djtz.now() - timedelta(days=30)

    # Rating distribution
    dist = [
        {"rating": r["service_rating"], "cnt": r["cnt"]}
        for r in (approved_claimed.values("service_rating")
                  .annotate(cnt=Count("id"))
                  .order_by("-service_rating"))
    ]

    # 6-month review trend — one grouped query instead of 6 monthly aggregates.
    now = djtz.now()
    from django.db.models.functions import TruncMonth
    month_stats = {
        (r["month"].year, r["month"].month): r
        for r in (claimed_reviews.annotate(month=TruncMonth("created_at"))
                  .values("month")
                  .annotate(cnt=Count("id"), avg=Avg("service_rating")))
    }
    trend = []
    for offset in range(5, -1, -1):
        anchor = (now.replace(day=1) - timedelta(days=offset * 30)).replace(day=1)
        ym = anchor.strftime("%Y-%m")
        st = month_stats.get((anchor.year, anchor.month))
        trend.append({
            "month_key": ym,
            "month_label": anchor.strftime("%b"),
            "cnt": st["cnt"] if st else 0,
            "avg_rating": round(float(st["avg"] or 0), 1) if st else 0,
        })

    # 30-day stock breakdown
    last_30_reviews = claimed_reviews.filter(created_at__gte=cutoff_30)
    stock_in  = last_30_reviews.filter(stock_available=1).count()
    stock_out = last_30_reviews.filter(stock_available=0).count()

    # Team / membership
    team_qs = (PharmacyTeamMember.objects.filter(org=org)
               .select_related("user").order_by("-role", "accepted_at")) if org else []
    team_for_template = [
        {"id": m.id, "email": m.email, "role": m.role,
         "accepted_at": m.accepted_at, "invited_at": m.invited_at,
         "user_id": m.user_id,
         "first_name": (m.user.first_name if m.user_id else ""),
         "last_name":  (m.user.last_name  if m.user_id else "")}
        for m in team_qs
    ]
    team_accepted_count = sum(1 for m in (team_qs if org else []) if m.accepted_at)

    response_leaders = []
    if org:
        from django.db.models import Max
        from ..models import ReviewResponse
        # Group by (responder_id, responder_name) to mirror prod's GROUP BY
        # responder_id with MAX(created_at) per group.
        leaders_qs = (ReviewResponse.objects.filter(org=org)
                      .values("responder_name")
                      .annotate(response_count=Count("id"),
                                last_response=Max("created_at"))
                      .order_by("-response_count"))
        response_leaders = list(leaders_qs)

    services = list(Pharmacy.objects.filter(id__in=claimed_ids)
                    .values_list("services__service_type", flat=True).distinct())
    services = [{"service_type": s} for s in services if s]

    hours = list(PharmacyHours.objects.filter(pharmacy_id__in=claimed_ids)
                 .order_by("day_of_week")
                 .values("day_of_week", "day_name", "open_time", "close_time", "is_closed"))

    insurance = list(PharmacyInsuranceProvider.objects.filter(pharmacy_id__in=claimed_ids)
                     .order_by("provider_name")
                     .values("id", "provider_name"))

    coverage = list(PharmacyCoveragePlan.objects.filter(pharmacy_id__in=claimed_ids)
                    .order_by("plan_name")
                    .values("id", "plan_name", "plan_type"))

    # Listing-completeness checklist (services → insurance → hours → reviews).
    # Drives the "next step" progress bar on the overview tab.
    services_ok = bool(services)
    insurance_ok = bool(insurance or coverage)
    hours_ok = bool(hours)
    reviews_ok = resp_total == 0 or resp_responded >= resp_total
    setup_steps = [
        {"key": "services", "label": "Add your services",
         "desc": "Delivery, drive-thru, compounding, online refills & more.",
         "url": "/pharmacist-dashboard?tab=services", "done": services_ok},
        {"key": "insurance", "label": "Add insurance & government plans",
         "desc": "The insurers, PBMs and Medicare/Medicaid plans you accept.",
         "url": "/pharmacist-dashboard?tab=insurance", "done": insurance_ok},
        {"key": "hours", "label": "Set operating hours",
         "desc": "Open/close times shown on your public listing.",
         "url": "/pharmacist-dashboard?tab=hours", "done": hours_ok},
        {"key": "reviews", "label": "Respond to reviews",
         "desc": "Reply to patient reviews to build trust with neighbors.",
         "url": "/pharmacist-dashboard?tab=reviews", "done": reviews_ok},
    ]
    # Annotate each step with the completion state of the step above it so the
    # vertical connector line turns green only once the step it leads from is done.
    for i in range(1, len(setup_steps)):
        setup_steps[i]["prev_done"] = setup_steps[i - 1]["done"]
    setup_done = sum(1 for s in setup_steps if s["done"])
    setup_pct = round(setup_done * 100 / len(setup_steps))
    setup_next = next((s for s in setup_steps if not s["done"]), None)

    # Once the checklist hits 100%, persist it and celebrate — exactly once.
    # listing_completed_at doubles as the source of truth for the Verified
    # badge; the confetti only fires on the first load where completion is seen.
    setup_confetti = False
    if primary_pharm and setup_pct == 100 and not primary_pharm.listing_completed_at:
        primary_pharm.listing_completed_at = djtz.now()
        primary_pharm.save(update_fields=["listing_completed_at"])
        PharmacyPublishable.objects.filter(pharmacy_id=primary_pharm.id).update(
            listing_completed_at=primary_pharm.listing_completed_at)
        setup_confetti = True

    return {
        # Public-facing context
        "org": Row({"id": org.id, "name": org.name}) if org else None,
        "pharmacies": [_pharmacy_dict(p) for p in my_pharms_qs],
        "recent_reviews": [_review_dict(r) for r in recent_reviews],
        "pending_reviews": Row({"count": sum(1 for r in recent_reviews if not r.response_text)}),
        "responses_used": Row({"total": responses_used, "limit": 10}),
        "team": team_for_template,
        "tab": tab,
        "welcome": request.GET.get("welcome"),
        "msg": request.GET.get("msg"),
        "error": request.GET.get("error"),
        # Prod-port aliases (templates iterate as `{% for item2 in X %}` etc.)
        "my_pharmacy":   ([_pharmacy_dict(primary_pharm)] if primary_pharm else []),
        "my_pid":        ([{"id": primary_pid,
                    "url": _pharmacy_dict(primary_pharm)["url"]}] if primary_pharm else []),
        "my_membership": ([{"org_name": org.name, "my_role": my_tm.role}] if (org and my_tm) else []),
        "my_org":        ([{"id": org.id, "name": org.name, "my_role": my_tm.role}] if (org and my_tm) else []),
        "resp_stats":    [{"total": resp_total, "responded": resp_responded}],
        "week_reviews":  [{"cnt": claimed_reviews.filter(created_at__gte=cutoff_7).count()}],
        "month_reviews": [{"cnt": claimed_reviews.filter(created_at__gte=cutoff_30).count()}],
        "dist":          dist,
        "trend":         trend,
        "latest":        list(approved_claimed.order_by("-created_at")[:5].values(
                             "user_name", "service_rating", "comment", "created_at")),
        "stock":         [{"in_stock": stock_in, "out_of_stock": stock_out,
                           "total": stock_in + stock_out}],
        "resp_count":    [{"total": responses_used}],
        "pharm_reviews": [_review_dict(r) for r in approved_claimed.order_by("-created_at")],
        "member_count":  [{"cnt": team_accepted_count}],
        "members":       team_for_template,
        "response_leaders": response_leaders,
        "widget_pid":    ([{"id": primary_pid}] if primary_pid else []),
        "widget_pid2":   ([{"id": primary_pid}] if primary_pid else []),
        "current_services": services,
        "svc_pid":       ([{"id": primary_pid}] if primary_pid else []),
        "my_hours":      hours,
        "current_insurance": insurance,
        "insurance_pid":  ([{"id": primary_pid}] if primary_pid else []),
        "current_coverage": coverage,
        "cov_pid":        ([{"id": primary_pid}] if primary_pid else []),
        "hours_pid":      ([{"id": primary_pid}] if primary_pid else []),
        "hours_map":      {str(h["day_of_week"]): h for h in hours},
        "setup_steps":    setup_steps,
        "setup_pct":      Row({"total": setup_pct}),
        "setup_done":     Row({"total": setup_done, "of": len(setup_steps)}),
        "setup_next":     Row(setup_next) if setup_next else None,
        "setup_confetti": setup_confetti,
    }


def page_admin_dashboard(request):
    now = djtz.now()

    flagged_qs = Review.objects.filter(
        moderation_status__in=("pending", "flagged"), deleted_at__isnull=True
    ).order_by("-created_at")[:4]

    pending_claims_qs = PharmacyClaim.objects.filter(status="pending").order_by("-created_at")[:4]

    top_pharms = Pharmacy.objects.filter(total_reviews__gt=0).order_by("-total_reviews")[:5]

    # 7-day review sparkline — a single grouped query instead of 7 COUNTs.
    from django.db.models.functions import TruncDate
    sparkline_days = [(now - timedelta(days=i)).date() for i in range(6, -1, -1)]
    day_counts = {
        r["day"]: r["c"]
        for r in (Review.objects
                  .filter(created_at__date__in=sparkline_days, deleted_at__isnull=True)
                  .annotate(day=TruncDate("created_at"))
                  .values("day").annotate(c=Count("id")))
    }
    sparkline = [{"label": d.strftime("%a"), "count": day_counts.get(d, 0)}
                 for d in sparkline_days]

    # Stat counts in two shapes — `stat_*` Row{} for the redesigned tiles,
    # and the legacy `*_cnt` / `*_rev` lists for the prod-port template
    # bits that haven't been refactored.
    n_pharm   = Pharmacy.objects.filter(status="active").count()
    n_users   = User.objects.filter(deactivated_at__isnull=True).count()
    n_reviews = Review.objects.filter(deleted_at__isnull=True).count()
    n_claims  = PharmacyClaim.objects.filter(status="pending").count()
    n_mod     = Review.objects.filter(moderation_status="pending").count()
    n_flagged = Review.objects.filter(moderation_status="flagged", deleted_at__isnull=True).count()
    n_short   = DrugShortage.objects.filter(status="current").count()
    avg_rating_val = Review.objects.filter(deleted_at__isnull=True).aggregate(v=Avg("service_rating"))["v"] or 0
    cutoff_30 = djtz.now() - timedelta(days=30)
    return {
        "stat_pharmacies": Row({"total": n_pharm}),
        "stat_users": Row({"total": n_users}),
        "stat_reviews": Row({"total": n_reviews}),
        "stat_claims_pending": Row({"total": n_claims}),
        "stat_mod_pending": Row({"total": n_mod}),
        "stat_flagged": Row({"total": n_flagged}),
        "stat_shortages": Row({"total": n_short}),
        "stat_closed_flags": Row({"total": ClosedFlag.objects.filter(status="pending").count()}),
        # Legacy aliases (admin-dashboard.html still references these in some tiles)
        "pharm_cnt":   [{"n": n_pharm}],
        "user_cnt":    [{"n": n_users}],
        "rev_cnt":     [{"n": n_reviews}],
        "claim_cnt":   [{"n": n_claims}],
        "avg_rating":  [{"avg": round(float(avg_rating_val), 1)}],
        "month_rev":   [{"n": Review.objects.filter(created_at__gte=cutoff_30, deleted_at__isnull=True).count()}],
        "month_users": [{"n": User.objects.filter(date_joined__gte=cutoff_30).count()}],
        "activity": [{"id": a.id, "type": a.type, "message": a.message,
                      "created_at": a.created_at, "user_id": a.user_id}
                     for a in ActivityLog.objects.order_by("-created_at")[:30]],
        "attention_reviews": [_review_dict(r) for r in flagged_qs],
        "attention_claims": [
            {"id": c.id, "pharmacy_name": c.pharmacy_name, "user_name": c.user_name,
             "npi_number": c.npi_number, "created_at": c.created_at}
            for c in pending_claims_qs
        ],
        "top_pharms": [_pharmacy_dict(p) for p in top_pharms],
        "sparkline": sparkline,
        "search_radius_mi": get_search_radius_mi(),
    }


def page_admin_analytics(request):
    now = djtz.now()
    last_30 = now - timedelta(days=30)
    reviews_30 = Review.objects.filter(created_at__gte=last_30, deleted_at__isnull=True)
    claims_30 = PharmacyClaim.objects.filter(created_at__gte=last_30)
    users_30 = User.objects.filter(date_joined__gte=last_30)
    per_day = []
    from django.db.models.functions import TruncDate
    last30_days = [(now - timedelta(days=i)).date() for i in range(29, -1, -1)]
    day_rev_counts = {
        r["day"]: r["c"]
        for r in (Review.objects
                  .filter(created_at__date__in=last30_days, deleted_at__isnull=True)
                  .annotate(day=TruncDate("created_at"))
                  .values("day").annotate(c=Count("id")))
    }
    for day in last30_days:
        per_day.append({"day": day.isoformat(), "reviews": day_rev_counts.get(day, 0)})
    n_users   = User.objects.count()
    n_reviews = Review.objects.filter(deleted_at__isnull=True).count()
    n_pharm   = Pharmacy.objects.filter(status="active").count()
    avg_val   = Review.objects.filter(deleted_at__isnull=True).aggregate(v=Avg("service_rating"))["v"] or 0
    top_pharms_qs = list(Pharmacy.objects.filter(status="active")
                         .order_by("-total_reviews")[:10])
    rating_dist = [
        {"rating": r["service_rating"], "count": r["count"]}
        for r in (Review.objects.filter(deleted_at__isnull=True)
                  .values("service_rating")
                  .annotate(count=Count("id"))
                  .order_by("-service_rating"))
    ]
    geo_pharms = list(Pharmacy.objects.filter(status="active", latitude__isnull=False)
                      .values("id", "name", "latitude", "longitude", "total_reviews"))
    return {
        "kpi_reviews_30d": Row({"total": reviews_30.count()}),
        "kpi_claims_30d": Row({"total": claims_30.count()}),
        "kpi_users_30d": Row({"total": users_30.count()}),
        "kpi_ncsales_30d": Row({"total": 0}),
        "review_trend": list(reversed(per_day)),
        "top_pharmacies": [_pharmacy_dict(p) for p in top_pharms_qs],
        # Legacy stat-tile + chart aliases the prod template still uses.
        "total_u":    [{"n": n_users}],
        "total_r":    [{"n": n_reviews}],
        "total_p":    [{"n": n_pharm}],
        "avg_r":      [{"avg": round(float(avg_val), 1)}],
        "top_pharms": [{"name": p.name, "total_reviews": p.total_reviews,
                        "avg_service_rating": p.avg_service_rating,
                        "stock_confidence": p.stock_confidence}
                       for p in top_pharms_qs],
        "dist":       rating_dist,
        "geo_pharms": geo_pharms,
    }


def page_admin_claims(request):
    status = (request.GET.get("status") or "").strip()
    pending = PharmacyClaim.objects.filter(status="pending").order_by("-created_at")
    recent = PharmacyClaim.objects.exclude(status="pending").order_by("-reviewed_at")[:20]
    all_qs = PharmacyClaim.objects.all()
    if status:
        all_qs = all_qs.filter(status=status)
    all_qs = all_qs.order_by(
        # pending → approved → other; matches prod CASE-ordering exactly.
        Case(When(status="pending", then=Value(0)),
             When(status="approved", then=Value(1)),
             default=Value(2)),
        "-created_at",
    )

    def _c(c):
        return {"id": c.id, "pharmacy_name": c.pharmacy_name, "user_name": c.user_name,
                "user_id": c.user_id, "npi_number": c.npi_number,
                "license_number": c.license_number, "status": c.status,
                "created_at": c.created_at, "rejection_reason": c.rejection_reason}
    return {
        "pending": [_c(c) for c in pending],
        "recent": [_c(c) for c in recent],
        "pending_count": Row({"total": pending.count()}),
        # Legacy aliases
        "pending_cnt": [{"n": pending.count()}],
        "claims": [_c(c) for c in all_qs],
    }


def page_admin_moderation(request):
    flagged = Review.objects.filter(moderation_status__in=("pending", "flagged")).order_by("-created_at")
    keywords = list(ModerationKeyword.objects.order_by("category", "keyword"))
    all_qs = (Review.objects.filter(deleted_at__isnull=True)
              .select_related("pharmacy").order_by("-created_at"))
    return {
        "flagged": [_review_dict(r) for r in flagged],
        "flagged_count": Row({"total": flagged.count()}),
        "keywords": [{"id": k.id, "keyword": k.keyword, "category": k.category}
                      for k in keywords],
        # Legacy alias — template renders {% for item in all_reviews %} for the
        # full moderation queue, with item.pharm_name from the joined pharmacy.
        "all_reviews": [{**_review_dict(r),
                         "pharm_name": (r.pharmacy.name if r.pharmacy_id else "")}
                        for r in all_qs],
    }


def page_admin_pharmacies(request):
    q = (request.GET.get("q") or "").strip()
    pub = PharmacyPublishable.objects
    qs = pub.all().order_by("name")
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(address__icontains=q) | Q(zip=q))
    active = pub.filter(status="active")
    chain_rows = []
    for key, label, qexp in BRAND_PATTERNS:
        n_active = active.filter(qexp).count()
        n_total  = pub.filter(qexp).count()
        if n_total > 0:
            chain_rows.append({
                "key":      key,
                "label":    label,
                "active":   n_active,
                "total":    n_total,
                "defunct":  key in DEFUNCT_CHAIN_KEYS,
            })
    chain_rows.sort(key=lambda r: (not r["defunct"], -r["active"]))

    # Attach the publish_status (from the publishable table, not the source
    # pharmacy) so the admin list + edit UI can show/hide the badge correctly.
    def _pub_dict(p):
        d = _pharmacy_dict(p)
        d["publish_status"] = p.publish_status
        return d

    # Pending pharmacies: source pharmacies that don't have a publishable row yet.
    # Ordered newest first so admins see the latest imports at the top.
    from django.core.paginator import Paginator
    pending_page = int(request.GET.get("pending_page") or 1)
    pending_qs = Pharmacy.objects.filter(
        status="active", deleted_at__isnull=True
    ).exclude(
        publishable__isnull=False
    ).order_by("-created_at")
    pending_paginator = Paginator(pending_qs, 25)
    pending_page_obj = pending_paginator.get_page(pending_page)

    def _src_dict(p):
        """Dict for a source Pharmacy (pending) — includes created_at and is_new flag."""
        from django.utils import timezone
        d = _pharmacy_dict(p)
        d["created_at"] = p.created_at
        d["publish_status"] = "pending"
        # NEW tag for pharmacies created within the last 7 days
        d["is_new"] = (timezone.now() - p.created_at).days < 7
        return d

    flags_qs = ClosedFlag.objects.filter(status="pending").select_related("pharmacy", "reporter_user").order_by("-created_at")
    flags_list = []
    for f in flags_qs[:200]:
        flags_list.append({
            "id": f.id,
            "pharmacy_id": f.pharmacy_id,
            "pharmacy_name": f.pharmacy.name if f.pharmacy else "Unknown",
            "pharmacy_city": f.pharmacy.city if f.pharmacy else "",
            "pharmacy_state": f.pharmacy.state if f.pharmacy else "",
            "pharmacy_address": f.pharmacy.address if f.pharmacy else "",
            "pharmacy_url": f"/pharmacy?id={f.pharmacy_id}",
            "reporter": f.reporter_user.email if f.reporter_user else (f"Anonymous ({f.reporter_ip})" if f.reporter_ip else "Anonymous"),
            "note": f.note,
            "created_at": f.created_at,
        })

    return {
        "pharmacies": [_pub_dict(p) for p in qs[:500]],
        "total_count": Row({"total": qs.count()}),
        "success": request.GET.get("success"),
        "error": request.GET.get("error"),
        "q": q,
        "tab": request.GET.get("tab", ""),
        # Legacy stat tiles + main list
        "phys_cnt": [{"n": active.filter(is_digital=0).count()}],
        "dig_cnt":  [{"n": active.filter(is_digital=1).count()}],
        "zip_cnt":  [{"n": active.exclude(zip__isnull=True).exclude(zip="")
                            .values("zip").distinct().count()}],
        "all_pharms": [_pub_dict(p) for p in
                       pub.filter(status="active")
                       .order_by("name")[:100]],
        "chain_rows": chain_rows,
        # Pending pharmacies (not yet published)
        "pending_pharmacies": [_src_dict(p) for p in pending_page_obj],
        "pending_page_obj": pending_page_obj,
        "pending_paginator": pending_paginator,
        # Flagged as closed
        "closed_flags": flags_list,
        "closed_flags_count": flags_qs.count(),
    }


def page_admin_shortages(request):
    qs = DrugShortage.objects.order_by("-updated_at")
    return {
        "shortages": [_shortage_dict(s) for s in qs[:500]],
        "total_count": Row({"total": qs.count()}),
        "success": request.GET.get("success"),
        "error": request.GET.get("error"),
    }


def page_admin_users(request):
    from django.db.models import Max
    q      = (request.GET.get("q")      or "").strip()
    role   = (request.GET.get("role")   or "").strip()
    status = (request.GET.get("status") or "").strip()
    sort   = (request.GET.get("sort")   or "newest").strip()

    qs = User.objects.annotate(review_count=Count("reviews", filter=Q(reviews__deleted_at__isnull=True)))
    if q:
        qs = qs.filter(
            Q(email__icontains=q) | Q(first_name__icontains=q) |
            Q(last_name__icontains=q) | Q(phone__icontains=q) |
            Q(zip_code__icontains=q)
        )
    if role == "suspended":
        qs = qs.filter(deactivated_at__isnull=False)
    elif role:
        qs = qs.filter(role=role, deactivated_at__isnull=True)
    elif status == "suspended":
        qs = qs.filter(deactivated_at__isnull=False)
    elif status == "active":
        qs = qs.filter(deactivated_at__isnull=True)

    sort_map = {
        "newest":  "-date_joined",
        "oldest":  "date_joined",
        "name":    "first_name",
        "reviews": "-review_count",
    }
    qs = qs.order_by(sort_map.get(sort, "-date_joined"))

    # Last activity per user — single query
    last_active_map = {
        r["user_id"]: r["last_at"]
        for r in ActivityLog.objects.values("user_id").annotate(last_at=Max("created_at"))
        if r["user_id"]
    }

    now = djtz.now()
    users_out = []
    for u in qs[:500]:
        users_out.append({
            "id": u.id, "email": u.email,
            "first_name": u.first_name or "", "last_name": u.last_name or "",
            "role": u.role, "phone": u.phone or "", "zip_code": u.zip_code or "",
            "deactivated_at": u.deactivated_at, "created_at": u.date_joined,
            "review_count": u.review_count,
            "last_active": last_active_map.get(u.id),
        })

    all_users = User.objects.all()
    stat_total      = all_users.count()
    stat_active     = all_users.filter(deactivated_at__isnull=True).count()
    stat_suspended  = all_users.filter(deactivated_at__isnull=False).count()
    stat_pharmacist = all_users.filter(role="pharmacist").count()
    stat_admin      = all_users.filter(role="admin").count()
    stat_new_month  = all_users.filter(date_joined__gte=now - timedelta(days=30)).count()

    return {
        "users": users_out,
        "total_count": Row({"total": len(users_out)}),
        "q": q, "role": role, "status": status, "sort": sort,
        "stat_total":      Row({"n": stat_total}),
        "stat_active":     Row({"n": stat_active}),
        "stat_suspended":  Row({"n": stat_suspended}),
        "stat_pharmacist": Row({"n": stat_pharmacist}),
        "stat_admin":      Row({"n": stat_admin}),
        "stat_new_month":  Row({"n": stat_new_month}),
    }
