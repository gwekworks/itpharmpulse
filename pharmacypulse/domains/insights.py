"""Industry-insights provider: page_insights (heavy cached aggregates)."""
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


def page_insights(request):
    """NPPES-grounded industry insights. Review data is intentionally light
    on this page — pharmacy facts come from the public NPI registry, FDA
    shortages from openFDA. The reviewed-pharmacies card is the only place
    user reviews show up."""
    cache_key = "pp:insights:v4"
    cached = cache.get(cache_key)
    if cached:
        return cached

    active = Pharmacy.objects.filter(status="active", deleted_at__isnull=True)

    total_pharm   = active.count()
    digital_n     = active.filter(is_digital=1).count()
    claimed_n     = active.exclude(claimed_by__isnull=True).count()
    states_n      = active.exclude(state="").exclude(state__isnull=True).values("state").distinct().count()
    zips_n        = active.exclude(zip="").exclude(zip__isnull=True).values("zip").distinct().count()
    # Rough density per 100k Americans (US pop ~334M).
    pharm_per_100k = round(total_pharm / 3340) if total_pharm else 0

    # States with the most pharmacies (population proxy, but still a
    # genuinely useful "where is the bulk of US pharmacy infrastructure" view).
    top_states = list(
        active.exclude(state="").exclude(state__isnull=True)
              .values("state").annotate(n=Count("id")).order_by("-n")[:10]
    )
    state_max = max((s["n"] for s in top_states), default=1)
    for s in top_states:
        s["pct"] = round(s["n"] / state_max * 100)
        s["name"] = _STATE_NAMES.get(s["state"], s["state"])

    # Independents reliance — states where the highest share of pharmacies
    # are independents (chain_size < 5). Filter to states with at least 50
    # pharmacies so a low-pop state with 4 stores doesn't dominate.
    state_indep = []
    state_rows = (
        active.exclude(state="").exclude(state__isnull=True)
              .values("state")
              .annotate(
                  total=Count("id"),
                  indep=Count("id", filter=Q(chain_size__lt=5, is_digital=0)),
              ).filter(total__gte=50)
    )
    for r in state_rows:
        pct = round((r["indep"] / r["total"]) * 100) if r["total"] else 0
        state_indep.append({
            "state": r["state"], "name": _STATE_NAMES.get(r["state"], r["state"]),
            "total": r["total"], "indep": r["indep"], "pct": pct,
        })
    state_indep.sort(key=lambda x: -x["pct"])
    state_indep = state_indep[:10]
    indep_max = max((s["pct"] for s in state_indep), default=1)
    for s in state_indep:
        s["bar"] = round(s["pct"] / indep_max * 100) if indep_max else 0

    # Chain mix.
    chain_breakdown = {
        "independent": active.filter(chain_size__lt=5, is_digital=0).count(),
        "regional":    active.filter(chain_size__gte=5, chain_size__lt=100, is_digital=0).count(),
        "national":    active.filter(chain_size__gte=100, is_digital=0).count(),
        "online":      digital_n,
    }
    chain_total = sum(chain_breakdown.values()) or 1
    chain_pct = {k: round(v / chain_total * 100) for k, v in chain_breakdown.items()}

    # Top pharmacy brands. The NPPES `name` field is messy ("WALGREENS #1234"),
    # so we coerce to a coarse brand by taking the leading word(s). The chain
    # list also drives the admin "Manage chains" panel — keep them in sync.
    brand_rows = []
    for key, label, q in BRAND_PATTERNS:
        n = active.filter(q).count()
        if n > 0:
            brand_rows.append({"label": label, "n": n})
    brand_rows.sort(key=lambda r: -r["n"])
    brand_rows = brand_rows[:8]
    brand_max = max((r["n"] for r in brand_rows), default=1)
    for r in brand_rows:
        r["bar"] = round(r["n"] / brand_max * 100)
        r["pct"] = round(r["n"] / total_pharm * 100, 1) if total_pharm else 0

    # Taxonomy / specialty mix (NPPES code).
    tax_rows = list(
        active.exclude(taxonomy_code="").exclude(taxonomy_code__isnull=True)
              .values("taxonomy_code").annotate(n=Count("id")).order_by("-n")[:8]
    )
    tax_total = sum(r["n"] for r in tax_rows) or 1
    tax_max = max((r["n"] for r in tax_rows), default=1)
    for r in tax_rows:
        r["label"] = _TAXONOMY_LABEL.get(r["taxonomy_code"], r["taxonomy_code"])
        r["pct"] = round(r["n"] / tax_total * 100)
        r["bar"] = round(r["n"] / tax_max * 100)

    # Pharmacy founding era — NPPES enumeration_date. Bucket by decade + an
    # "opened in last 5y" tally for the sub-headline. SQL aggregation so we
    # don't ship 79k rows back to Python on cold-cache (was a 30s timeout).
    # enumeration_date is YYYY-MM-DD text, so lexicographic comparison ==
    # chronological comparison.
    cutoff_year = djtz.now().year - 5
    cutoff_date = f"{cutoff_year}-01-01"
    era_agg = active.exclude(enumeration_date__isnull=True).aggregate(
        pre2000  = Count("id", filter=Q(enumeration_date__lt="2000-01-01")),
        s2000    = Count("id", filter=Q(enumeration_date__gte="2000-01-01") & Q(enumeration_date__lt="2010-01-01")),
        s2010    = Count("id", filter=Q(enumeration_date__gte="2010-01-01") & Q(enumeration_date__lt="2020-01-01")),
        s2020    = Count("id", filter=Q(enumeration_date__gte="2020-01-01")),
        recent5y = Count("id", filter=Q(enumeration_date__gte=cutoff_date)),
    )
    era_buckets = {
        "pre2000": era_agg["pre2000"] or 0,
        "2000s":   era_agg["s2000"]   or 0,
        "2010s":   era_agg["s2010"]   or 0,
        "2020s":   era_agg["s2020"]   or 0,
    }
    recent_5y = era_agg["recent5y"] or 0
    era_total = sum(era_buckets.values()) or 1
    era_max = max(era_buckets.values(), default=1) or 1
    era_chart = [
        {"label": "Pre-2000", "n": era_buckets["pre2000"], "pct": round(era_buckets["pre2000"]/era_total*100), "bar": round(era_buckets["pre2000"]/era_max*100)},
        {"label": "2000s",    "n": era_buckets["2000s"],   "pct": round(era_buckets["2000s"]/era_total*100),   "bar": round(era_buckets["2000s"]/era_max*100)},
        {"label": "2010s",    "n": era_buckets["2010s"],   "pct": round(era_buckets["2010s"]/era_total*100),   "bar": round(era_buckets["2010s"]/era_max*100)},
        {"label": "2020s",    "n": era_buckets["2020s"],   "pct": round(era_buckets["2020s"]/era_total*100),   "bar": round(era_buckets["2020s"]/era_max*100)},
    ]
    recent_5y_pct = round(recent_5y / era_total * 100) if era_total else 0

    # Hours coverage from PharmacyHours rows. Pharmacies with NO hours rows
    # at all are excluded so the percentages stay honest (NPPES doesn't ship
    # hours — only Google-enriched rows have them).
    pharms_with_hours = PharmacyHours.objects.values("pharmacy_id").distinct().count()
    weekend_open_pharms = (
        PharmacyHours.objects
        .filter(day_of_week__in=(0, 6), is_closed=0)
        .values("pharmacy_id").distinct().count()
    )
    sunday_open_pharms = (
        PharmacyHours.objects
        .filter(day_of_week=0, is_closed=0)
        .values("pharmacy_id").distinct().count()
    )
    seven_day_pharms = (
        PharmacyHours.objects.filter(is_closed=0)
        .values("pharmacy_id").annotate(days=Count("day_of_week", distinct=True))
        .filter(days__gte=7).count()
    )
    weekend_pct = round(weekend_open_pharms / pharms_with_hours * 100) if pharms_with_hours else 0
    sunday_pct  = round(sunday_open_pharms  / pharms_with_hours * 100) if pharms_with_hours else 0
    seven_pct   = round(seven_day_pharms    / pharms_with_hours * 100) if pharms_with_hours else 0

    # Drug shortages (external FDA data — kept).
    shortages = DrugShortage.objects.all()
    short_total    = shortages.count()
    short_current  = shortages.filter(status="current").count()
    short_resolved = shortages.filter(status="resolved").count()
    short_disc     = shortages.exclude(status__in=("current", "resolved")).count()
    top_current_shortages = list(
        shortages.filter(status="current")
                 .values("drug_name", "generic_name", "manufacturer",
                          "reason", "estimated_resolution")[:6]
    )
    top_reasons = list(
        shortages.exclude(reason="").exclude(reason__isnull=True)
                 .values("reason").annotate(n=Count("id")).order_by("-n")[:5]
    )
    reason_max = max((r["n"] for r in top_reasons), default=1)
    for r in top_reasons:
        r["pct"] = round(r["n"] / reason_max * 100)
        r["short"] = (r["reason"] or "")[:80]

    # Most-reviewed pharmacies — only place review data appears.
    top_reviewed = [
        _pharmacy_dict(p) for p in
        active.filter(total_reviews__gt=0).order_by("-total_reviews", "-avg_service_rating")[:8]
    ]

    ctx = {
        "stat_pharm":     Row({"n": total_pharm}),
        "stat_states":    Row({"n": states_n}),
        "stat_zips":      Row({"n": zips_n}),
        "stat_short":     Row({"n": short_current}),
        "pharm_per_100k": pharm_per_100k,
        "claimed_n":      claimed_n,
        "claimed_pct":    round(claimed_n / total_pharm * 100) if total_pharm else 0,
        "top_states":     top_states,
        "state_indep":    state_indep,
        "chain_breakdown": chain_breakdown,
        "chain_pct":      chain_pct,
        "brand_rows":     brand_rows,
        "tax_rows":       tax_rows,
        "tax_total":      tax_total,
        "era_chart":      era_chart,
        "era_total":      era_total if sum(era_buckets.values()) else 0,
        "recent_5y":      recent_5y,
        "recent_5y_pct":  recent_5y_pct,
        "pharms_with_hours":   pharms_with_hours,
        "weekend_open_pharms": weekend_open_pharms,
        "weekend_pct":    weekend_pct,
        "sunday_pct":     sunday_pct,
        "seven_pct":      seven_pct,
        "short_total":    short_total,
        "short_current":  short_current,
        "short_resolved": short_resolved,
        "short_disc":     short_disc,
        "short_current_pct":  round(short_current  / short_total * 100) if short_total else 0,
        "short_resolved_pct": round(short_resolved / short_total * 100) if short_total else 0,
        "short_disc_pct":     round(short_disc     / short_total * 100) if short_total else 0,
        "top_current_shortages": top_current_shortages,
        "top_reasons":    top_reasons,
        "top_reviewed":   top_reviewed,
    }
    cache.set(cache_key, ctx, timeout=6 * 3600)  # 6h — NPPES data barely changes
    return ctx
