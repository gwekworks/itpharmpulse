"""Homepage providers: page_index (landing), page_home (authed)."""
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


_INDEX_AGG_CACHE_KEY = "page_index:aggregates:v1"


_INDEX_AGG_TTL = 300


def _index_aggregates():
    """Aggregates that don't depend on the requesting user — stat_*, mw_*, and
    hero_reviews. Cached for 5 minutes to keep the homepage off the 79k-row
    pharmacy table on every hit.

    Returns a flat dict shaped to drop straight into the page_index context.
    """
    cached = cache.get(_INDEX_AGG_CACHE_KEY)
    if cached is not None:
        return cached

    active = published_pharmacies()
    approved_reviews = Review.objects.filter(
        deleted_at__isnull=True, moderation_status="approved",
    )
    reviews_with_comments = approved_reviews.exclude(
        comment__isnull=True).exclude(comment="")

    hero_reviews = [{
        "user_name": r.user_name or "", "service_rating": r.service_rating,
        "comment": r.comment or "",
        "pharmacy_name": r.pharmacy.name if r.pharmacy_id else (r.pharmacy_name or ""),
    } for r in reviews_with_comments.select_related("pharmacy").order_by("-created_at")[:10]]

    # Single aggregate over the active queryset for all four stat tiles —
    # one query instead of four full-table COUNTs.
    stats = active.aggregate(
        n_pharm=Count("pk"),
        n_zip=Count("zip", distinct=True, filter=~Q(zip__isnull=True) & ~Q(zip="")),
    )
    review_stats = approved_reviews.aggregate(
        n_rev=Count("id"),
        avg=Avg("service_rating"),
    )
    avg_rating = review_stats["avg"] or 0

    mw_s_val = active.filter(stock_confidence__gt=0).aggregate(
        v=Avg("stock_confidence"))["v"] or 0
    mw_t_val = active.filter(avg_service_rating__gt=0).aggregate(
        v=Avg("avg_service_rating"))["v"] or 0
    mw_w_val = active.filter(avg_wait_time__gt=0).aggregate(
        v=Avg("avg_wait_time"))["v"] or 0

    mw_sr = [{"name": p.name, "stock_confidence": p.stock_confidence}
             for p in active.filter(stock_confidence__gt=0)
                            .order_by("-stock_confidence")[:5]]
    mw_tr = [{"name": p.name, "avg_service_rating": p.avg_service_rating,
              "pct": round(p.avg_service_rating * 20)}
             for p in active.filter(avg_service_rating__gt=0)
                            .order_by("-avg_service_rating")[:5]]
    mw_wr = [{"name": p.name, "avg_wait_time": p.avg_wait_time}
             for p in active.filter(avg_wait_time__gt=0)
                            .order_by("avg_wait_time")[:5]]

    out = {
        "hero_reviews": hero_reviews,
        "t2_reviews": hero_reviews,  # prod re-queries the same data
        "stat_pharm": [{"n": stats["n_pharm"]}],
        "stat_rev": [{"n": review_stats["n_rev"]}],
        "stat_avg": [{"n": round(float(avg_rating), 1)}],
        "stat_zip": [{"n": stats["n_zip"]}],
        "mw_s": [{"v": round(float(mw_s_val))}],
        "mw_sr": mw_sr,
        "mw_t": [{"v": round(float(mw_t_val), 1), "pct": round(float(mw_t_val) * 20)}],
        "mw_tr": mw_tr,
        "mw_w": [{"v": round(float(mw_w_val))}],
        "mw_wr": mw_wr,
        "pharmacy_facts_json": _pharmacy_facts_json(),
    }
    cache.set(_INDEX_AGG_CACHE_KEY, out, _INDEX_AGG_TTL)
    return out


def _index_testimonials():
    """Random real reviews for the homepage testimonials grid.

    Deliberately NOT part of the cached _index_aggregates: re-queried on every
    request so the section shows a different set of reviews on each pageview.
    """
    from django.utils.timesince import timesince

    now = djtz.now()
    approved = Review.objects.filter(
        deleted_at__isnull=True, moderation_status="approved",
    ).exclude(comment__isnull=True).exclude(comment="")
    picks = approved.select_related("pharmacy").order_by("?")[:2]

    testimonials = []
    for r in picks:
        service = r.service_rating or 0
        if service >= 4:
            tag_class, tag_label = "pp-testimonial-tag-green", f"CARE {service}/5"
        else:
            wait = r.wait_time_rating or 3
            if wait <= 3:
                tag_class, tag_label = "pp-testimonial-tag-red", f"WAIT {wait}/5"
            else:
                tag_class, tag_label = "pp-testimonial-tag-red", f"SERVICE {service}/5"
        pharmacy = r.pharmacy.name if r.pharmacy_id else (r.pharmacy_name or "")
        testimonials.append({
            "name": (r.user_name or "").strip() or "Anonymous",
            "meta": " · ".join(
                filter(None, [pharmacy, timesince(r.created_at, now) + " ago"])),
            "tag_class": tag_class,
            "tag_label": tag_label,
            "quote": (r.comment or "").strip(),
        })
    return testimonials


def _index_cards(active, approved_reviews, loc):
    """Pick 1 closest retail + 1 closest online + 1 next-closest, then
    hydrate each card with latest comment and review count.

    With 78k retail pharmacies in the DB, the previous version loaded all of
    them into Python and haversine-sorted on every request. The bounding-box
    prefilter caps the candidate set to a few hundred even in dense regions
    (and degrades gracefully to a wider box, then rating-fallback, when the
    user is rural or has no resolvable location).
    """
    retail_pool = []
    online_pool = []

    if loc is not None:
        from math import radians, sin, cos, asin, sqrt
        ulat, ulng = loc

        def _km(p):
            if p.latitude is None or p.longitude is None:
                return float("inf")
            la1, lo1, la2, lo2 = map(radians,
                                     (ulat, ulng, p.latitude, p.longitude))
            a = sin((la2-la1)/2)**2 + cos(la1)*cos(la2)*sin((lo2-lo1)/2)**2
            return 2 * 6371 * asin(sqrt(a))

        # Try a tight bbox first, then widen. ±1° ≈ 110km, ±5° ≈ 550km.
        # We only need a single closest hit per bucket, so a few hundred
        # candidates is plenty.
        for delta in (1.0, 5.0):
            candidates = active.filter(
                latitude__gte=ulat - delta, latitude__lte=ulat + delta,
                longitude__gte=ulng - delta, longitude__lte=ulng + delta,
                latitude__isnull=False, longitude__isnull=False,
            )
            retail_pool = sorted(candidates.filter(is_digital=0), key=_km)
            if retail_pool:
                break

        # Online pharmacies are a small bucket (~1k); cheap to load all and
        # sort in Python rather than fight bbox edge cases.
        online_pool = sorted(
            list(active.filter(is_digital=1, latitude__isnull=False,
                               longitude__isnull=False)),
            key=_km,
        )

    if not retail_pool:
        retail_pool = list(active.filter(is_digital=0)
                           .order_by("-avg_service_rating", "-total_reviews")[:3])
    if not online_pool:
        online_pool = list(active.filter(is_digital=1)
                           .order_by("-avg_service_rating", "-total_reviews")[:3])

    best_retail = retail_pool[:1]
    best_online = online_pool[:1]
    picked_ids = {p.id for p in best_retail + best_online}
    remaining = [p for p in retail_pool + online_pool if p.id not in picked_ids]
    top3 = best_retail + best_online + remaining[:1]

    # Hydrate review_count + latest comment in two queries instead of 6.
    top3_ids = [p.id for p in top3]
    counts = dict(approved_reviews.filter(pharmacy_id__in=top3_ids)
                  .values("pharmacy_id")
                  .annotate(c=Count("id"))
                  .values_list("pharmacy_id", "c"))
    latest_by_pid: dict[int, Review] = {}
    for r in (approved_reviews.filter(pharmacy_id__in=top3_ids)
              .exclude(comment__isnull=True).exclude(comment="")
              .order_by("pharmacy_id", "-created_at")
              .only("pharmacy_id", "comment", "user_name", "created_at")):
        if r.pharmacy_id not in latest_by_pid:
            latest_by_pid[r.pharmacy_id] = r

    cards = []
    for p in top3:
        latest = latest_by_pid.get(p.id)
        cards.append({
            "id": p.id, "name": p.name, "city": p.city, "state": p.state,
            "zip": p.zip, "address": p.address,
            "stock_confidence": p.stock_confidence,
            "avg_wait_time": p.avg_wait_time,
            "avg_service_rating": p.avg_service_rating,
            "total_reviews": p.total_reviews, "is_digital": p.is_digital,
            "claimed_by": p.claimed_by,
            "review_count": counts.get(p.id, 0),
            "latest_comment": (latest.comment if latest else ""),
            "latest_user": (latest.user_name if latest else ""),
        })
    return cards


def page_index(request):
    """Matches the Benmore prod index.html query aliases 1:1:
      hero_reviews, t2_reviews  → list[{user_name, service_rating, comment, pharmacy_name}]
      stat_pharm, stat_rev, stat_avg, stat_zip → [{n: value}]
      cards                    → top-3 pharmacies (retail/online/other)
      mw_s/mw_sr/mw_t/mw_tr/mw_w/mw_wr → stock + service + wait widgets + top-5 lists
    Prod query shapes: each aggregate returns a 1-element list so `{{alias.0.n}}` resolves.
    """
    from ..geo import resolve as resolve_loc

    active = published_pharmacies()
    approved_reviews = Review.objects.filter(
        deleted_at__isnull=True, moderation_status="approved",
    )

    user_loc = resolve_loc(request)
    loc = (user_loc.lat, user_loc.lng) if (user_loc.lat and user_loc.lng) else None

    return {
        **_index_aggregates(),
        "cards": _index_cards(active, approved_reviews, loc),
        "testimonials": _index_testimonials(),
    }


def _pharmacy_facts_json() -> str:
    """Serialize active pharmacy facts for the homepage facts widget.

    Rendered in the template via Django's `json_script` filter, which
    HTML-escapes the JSON (including `</script>`), so admin-edited fact text
    can never break out of the <script> element."""
    import json
    from ..models import PharmacyFact
    facts = [
        {"icon": f.icon, "stat": f.stat, "head": f.head, "body": f.body,
         "cta": f.cta, "src": f.src, "cta_link": f.cta_link}
        for f in PharmacyFact.objects.filter(is_active=True)
    ]
    return json.dumps(facts)


def page_home(request):
    from ..geo import resolve as resolve_loc
    user = request.user
    # Resolution chain: authed zip → IP fallback. Same precedence as /list.
    auto_loc = resolve_loc(request)
    nearby = published_pharmacies()
    if auto_loc.zip:
        nearby = nearby.filter(zip__startswith=auto_loc.zip[:3])
    nearby = [_pharmacy_dict(p) for p in nearby.order_by("-avg_service_rating")[:8]]
    my_reviews = [_review_dict(r) for r in Review.objects.filter(
        user=user, deleted_at__isnull=True,
    ).order_by("-created_at")[:5]]
    shortages = [_shortage_dict(s) for s in DrugShortage.objects.filter(
        status="current").order_by("-updated_at")[:5]]
    return {
        "nearby": nearby,
        "my_reviews": my_reviews,
        "my_review_count": Row({"total": len(my_reviews)}),
        "shortages": shortages,
        # Stat tiles read {{pharm_count.0.count}} / {{review_count.0.count}}
        # — keep the [{count: N}] shape the template expects.
        "pharm_count": [{"count": published_pharmacies().count()}],
        "review_count": [{"count": Review.objects.filter(deleted_at__isnull=True).count()}],
        "auto_loc_zip":    auto_loc.zip,
        "auto_loc_city":   auto_loc.city,
        "auto_loc_state":  auto_loc.state,
        "auto_loc_source": auto_loc.source,
    }
