"""Pharmacy admin ops: bulk delete, data reporting, services, compare."""
from __future__ import annotations

import json
import re
import secrets
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from django.conf import settings
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db import transaction
from django.db.models import F, Sum
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse, HttpResponseBadRequest
from django.shortcuts import redirect
from django.utils import timezone as djtz
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from ...models import (
    ActivityLog, DataRequest, DrugShortage, NewsletterSubscriber, Notification,
    Pharmacy, PharmacyClaim, PharmacyCoveragePlan, PharmacyHours,
    PharmacyInsuranceProvider, PharmacyOrg, PharmacyTeamMember, ResponseCount,
    Review, ReviewResponse, SavedComparison, User, UserConsent,
)

from .common import admin_required, _activity


@admin_required
def bulk_delete_pharmacies(request):
    ids = request.POST.getlist("pharmacy_id")
    ids = [int(x) for x in ids if x.isdigit()][:500]  # hard cap
    if not ids:
        return redirect("/admin-pharmacies?error=No+pharmacies+selected")
    n = Pharmacy.objects.filter(id__in=ids).update(
        status="deleted", deleted_at=djtz.now(),
    )
    _activity(request, "pharmacy_bulk_delete",
              f"{request.user.email} soft-deleted {n} pharmacies")
    return redirect(f"/admin-pharmacies?success=Deleted+{n}+pharmacies")


@admin_required
def bulk_delete_chain(request):
    from ... import page_data
    from django.core.cache import cache
    chain_key = (request.POST.get("chain") or "").strip()
    entry = page_data.BRAND_PATTERNS_BY_KEY.get(chain_key)
    if not entry:
        return redirect("/admin-pharmacies?error=Unknown+chain")
    label, qexp = entry
    confirm = (request.POST.get("confirm") or "").strip().lower()
    if confirm != chain_key.lower():
        return redirect("/admin-pharmacies?error=Type+the+chain+key+to+confirm")
    # Soft-delete every active row in that chain. Reviews stay linked.
    n = Pharmacy.objects.filter(qexp, status="active").update(
        status="closed", deleted_at=djtz.now(),
    )
    _activity(request, "chain_bulk_delete",
              f"{request.user.email} soft-closed {n} {label} pharmacies")
    # Invalidate the /insights cache so the brands chart updates immediately.
    cache.delete("pp:insights:v4")
    return redirect(f"/admin-pharmacies?success=Soft-closed+{n}+{label}+pharmacies")


_DATA_ISSUES = {"address", "hours", "phone", "closed", "duplicate", "wrong_info", "other"}


_NOTE_MAX = 500


_NOTE_BAD_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _clean_note(s: str) -> str:
    s = (s or "")[:_NOTE_MAX]
    s = _NOTE_BAD_CHARS.sub("", s)
    s = s.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def report_pharmacy_data(request, pharmacy_id: int):
    from ...views_extra import is_rate_limited
    safe_next = f"/pharmacy?id={pharmacy_id}"

    # (2) Honeypot — any non-empty value means a bot filled the hidden field.
    if (request.POST.get("hp_email") or "").strip():
        # Pretend success; don't tip off the bot that we caught it.
        return redirect(f"{safe_next}&report=ok")

    # (3) Submit-too-fast guard. Form embeds a hidden epoch-second timestamp at
    # render time; legitimate submissions take at least a couple seconds.
    try:
        ts = int(request.POST.get("ts") or "0")
        if ts and (djtz.now().timestamp() - ts) < 2:
            return redirect(f"{safe_next}&report=ok")  # silent drop
    except (TypeError, ValueError):
        pass

    # (4a) Global IP rate limit.
    if is_rate_limited(request, "report_pharmacy", limit=10, window_s=3600):
        return redirect(f"{safe_next}&error=Too+many+reports.+Try+again+later.")
    # (4b) Per-(IP, pharmacy) rate limit — stop pile-on attacks on one row.
    if is_rate_limited(request, f"report_pharmacy_p{pharmacy_id}", limit=3, window_s=3600):
        return redirect(f"{safe_next}&error=You%27ve+already+reported+this+pharmacy.")

    # (5) Whitelist + cap.
    issue = (request.POST.get("issue") or "other").strip()
    if issue not in _DATA_ISSUES:
        issue = "other"
    # (6) Clean note.
    note = _clean_note(request.POST.get("note") or "")

    pharm = Pharmacy.objects.filter(id=pharmacy_id).only("id", "name").first()
    if not pharm:
        # Don't leak existence — same redirect either way.
        return redirect(f"{safe_next}&report=ok")

    actor = request.user.email if request.user.is_authenticated else "anonymous"
    user_id = request.user.id if request.user.is_authenticated else None
    # Sanitize the pharmacy name into the log too — NPPES data has been clean
    # historically but it's another untrusted source from the admin's POV.
    safe_name = _clean_note(pharm.name)[:100]
    ActivityLog.objects.create(
        type="pharmacy_data_report",
        user_id=user_id,
        message=(
            f"#{pharmacy_id} {safe_name}: {issue} (by {actor})"
            + (f" — {note}" if note else "")
        ),
    )
    # (7) Never honor `next` from the request — always send back to the
    # pharmacy page. Closes the open-redirect vector.
    return redirect(f"{safe_next}&report=ok")


@login_required
def add_to_compare(request):
    try:
        pharmacy_id = int(request.POST.get("pharmacy_id"))
    except (TypeError, ValueError):
        return HttpResponseBadRequest("pharmacy_id required")
    SavedComparison.objects.get_or_create(user=request.user, pharmacy_id=pharmacy_id)
    return JsonResponse({"ok": True})


@login_required
def remove_from_compare(request):
    try:
        pharmacy_id = int(request.POST.get("pharmacy_id"))
    except (TypeError, ValueError):
        return HttpResponseBadRequest("pharmacy_id required")
    SavedComparison.objects.filter(user=request.user, pharmacy_id=pharmacy_id).delete()
    return JsonResponse({"ok": True})


@login_required
def clear_compare(request):
    SavedComparison.objects.filter(user=request.user).delete()
    return redirect("/compare")


@login_required
def pharmacy_services_add(request):
    """POST handler for the services-tab form on /pharmacist-dashboard.

    Accepts one or more service types at once (the `service_type` checkbox
    list) plus an optional custom service (`service_type_custom`)."""
    from ...models import PharmacyService
    pid = request.POST.get("pharmacy_id")
    types = [t.strip() for t in request.POST.getlist("service_type")]
    custom = (request.POST.get("service_type_custom") or "").strip()
    if custom:
        types.append(custom)
    types = list(dict.fromkeys(t for t in types if t))
    if not (pid and types):
        return redirect("/pharmacist-dashboard?tab=services&error=Missing+fields")
    pharmacy = Pharmacy.objects.filter(id=pid).first()
    if not pharmacy or pharmacy.claimed_by != request.user.id:
        return redirect("/pharmacist-dashboard?tab=services&error=Not+authorized")
    for t in types:
        PharmacyService.objects.get_or_create(pharmacy=pharmacy, service_type=t)
    _activity(request, "service_added", f"Added services: {', '.join(types)}")
    return redirect("/pharmacist-dashboard?tab=services&msg=Services+added")


@login_required
def pharmacy_services_remove(request, service_id: int):
    from ...models import PharmacyService
    s = PharmacyService.objects.filter(id=service_id).select_related("pharmacy").first()
    if not s:
        return redirect("/pharmacist-dashboard?tab=services&error=Not+found")
    if s.pharmacy.claimed_by != request.user.id:
        return redirect("/pharmacist-dashboard?tab=services&error=Not+authorized")
    s.delete()
    return redirect("/pharmacist-dashboard?tab=services&msg=Service+removed")


_POPULAR_COVERAGE_PLANS = (
    ("Medicare Part D", "Medicare"),
    ("Medicare Advantage", "Medicare"),
    ("Medicaid", "Medicaid"),
    ("Tricare", "VA / Tricare"),
    ("Veterans Affairs (VA)", "VA / Tricare"),
    ("Marketplace / Exchange", "Marketplace / Exchange"),
    ("Commercial / Employer Plans", "Commercial / PBM"),
    ("Catastrophic Plans", "Other"),
)

_POPULAR_INSURANCE_COMPANIES = (
    "Aetna",
    "Blue Cross Blue Shield",
    "Caremark (CVS)",
    "Cigna",
    "Express Scripts",
    "Humana",
    "Kaiser Permanente",
    "Molina Healthcare",
    "OptumRx",
    "Oscar Health",
    "UnitedHealthcare",
    "Wellcare",
)


@login_required
def coverage_plan_add(request):
    """POST handler for the coverage-plans tab form on /pharmacist-dashboard.

    Accepts one or more plan names at once: the common-plan checkboxes
    (`plan_name` list) plus an optional custom plan (`plan_name_custom` with
    `plan_type`)."""
    pid = request.POST.get("pharmacy_id")
    names = [n.strip() for n in request.POST.getlist("plan_name")]
    custom = (request.POST.get("plan_name_custom") or "").strip()
    if custom:
        names.append(custom)
    names = list(dict.fromkeys(n for n in names if n))
    if not (pid and names):
        return redirect("/pharmacist-dashboard?tab=insurance&error=Missing+fields")
    pharmacy = Pharmacy.objects.filter(id=pid).first()
    if not pharmacy or pharmacy.claimed_by != request.user.id:
        return redirect("/pharmacist-dashboard?tab=insurance&error=Not+authorized")
    type_map = dict(_POPULAR_COVERAGE_PLANS)
    custom_type = (request.POST.get("plan_type") or "").strip()
    for name in names:
        PharmacyCoveragePlan.objects.get_or_create(
            pharmacy=pharmacy, plan_name=name,
            defaults={"plan_type": type_map.get(name, custom_type)},
        )
    _activity(request, "coverage_added", f"Added coverage plans: {', '.join(names)}")
    return redirect("/pharmacist-dashboard?tab=insurance&msg=Plans+added")


@login_required
def coverage_plan_remove(request, plan_id: int):
    i = PharmacyCoveragePlan.objects.filter(id=plan_id).select_related("pharmacy").first()
    if not i:
        return redirect("/pharmacist-dashboard?tab=insurance&error=Not+found")
    if i.pharmacy.claimed_by != request.user.id:
        return redirect("/pharmacist-dashboard?tab=insurance&error=Not+authorized")
    i.delete()
    return redirect("/pharmacist-dashboard?tab=insurance&msg=Plan+removed")


@login_required
def insurance_provider_add(request):
    """POST handler for the insurance tab form on /pharmacist-dashboard.

    Accepts one or more provider names at once: the popular-company checkboxes
    (`provider_name` list) plus an optional custom provider
    (`provider_name_custom`)."""
    pid = request.POST.get("pharmacy_id")
    names = [n.strip() for n in request.POST.getlist("provider_name")]
    custom = (request.POST.get("provider_name_custom") or "").strip()
    if custom:
        names.append(custom)
    names = list(dict.fromkeys(n for n in names if n))
    if not (pid and names):
        return redirect("/pharmacist-dashboard?tab=insurance&error=Missing+fields")
    pharmacy = Pharmacy.objects.filter(id=pid).first()
    if not pharmacy or pharmacy.claimed_by != request.user.id:
        return redirect("/pharmacist-dashboard?tab=insurance&error=Not+authorized")
    for name in names:
        PharmacyInsuranceProvider.objects.get_or_create(
            pharmacy=pharmacy, provider_name=name,
        )
    _activity(request, "insurance_added", f"Added insurance providers: {', '.join(names)}")
    return redirect("/pharmacist-dashboard?tab=insurance&msg=Providers+added")


@login_required
def insurance_provider_remove(request, provider_id: int):
    i = PharmacyInsuranceProvider.objects.filter(id=provider_id).select_related("pharmacy").first()
    if not i:
        return redirect("/pharmacist-dashboard?tab=insurance&error=Not+found")
    if i.pharmacy.claimed_by != request.user.id:
        return redirect("/pharmacist-dashboard?tab=insurance&error=Not+authorized")
    i.delete()
    return redirect("/pharmacist-dashboard?tab=insurance&msg=Provider+removed")


@login_required
@require_POST
def insurance_plans_add(request):
    """POST handler for the merged Insurance tab form on /pharmacist-dashboard.

    One form saves both insurance providers (checkbox `provider_name` list +
    custom `provider_name_custom`) and government coverage plans
    (`plan_name` list + custom `plan_name_custom` with `plan_type`)."""
    pid = request.POST.get("pharmacy_id")
    pharmacy = Pharmacy.objects.filter(id=pid).first()
    if not pharmacy or pharmacy.claimed_by != request.user.id:
        return redirect("/pharmacist-dashboard?tab=insurance&error=Not+authorized")

    provider_names = [n.strip() for n in request.POST.getlist("provider_name")]
    custom_provider = (request.POST.get("provider_name_custom") or "").strip()
    if custom_provider:
        provider_names.append(custom_provider)
    provider_names = list(dict.fromkeys(n for n in provider_names if n))
    for name in provider_names:
        PharmacyInsuranceProvider.objects.get_or_create(
            pharmacy=pharmacy, provider_name=name,
        )

    plan_names = [n.strip() for n in request.POST.getlist("plan_name")]
    custom_plan = (request.POST.get("plan_name_custom") or "").strip()
    if custom_plan:
        plan_names.append(custom_plan)
    plan_names = list(dict.fromkeys(n for n in plan_names if n))
    type_map = dict(_POPULAR_COVERAGE_PLANS)
    custom_type = (request.POST.get("plan_type") or "").strip()
    for name in plan_names:
        PharmacyCoveragePlan.objects.get_or_create(
            pharmacy=pharmacy, plan_name=name,
            defaults={"plan_type": type_map.get(name, custom_type)},
        )

    if not (provider_names or plan_names):
        return redirect("/pharmacist-dashboard?tab=insurance&error=Missing+fields")
    if plan_names:
        _activity(request, "coverage_added", f"Added coverage plans: {', '.join(plan_names)}")
    if provider_names:
        _activity(request, "insurance_added",
                  f"Added insurance providers: {', '.join(provider_names)}")
    return redirect("/pharmacist-dashboard?tab=insurance&msg=Insurance+saved")


_HOURS_DAYS = (
    (0, "Monday"), (1, "Tuesday"), (2, "Wednesday"),
    (3, "Thursday"), (4, "Friday"), (5, "Saturday"), (6, "Sunday"),
)


@login_required
def pharmacy_hours_save(request):
    """POST handler for the hours-tab form on /pharmacist-dashboard.

    Upserts one PharmacyHours row per weekday from the form's day_* fields so
    owners can add or edit hours when Google Places hasn't ingested them."""
    from ...models import PharmacyHours
    pid = request.POST.get("pharmacy_id")
    pharmacy = Pharmacy.objects.filter(id=pid).first()
    if not pharmacy or pharmacy.claimed_by != request.user.id:
        return redirect("/pharmacist-dashboard?tab=hours&error=Not+authorized")
    for day, day_name in _HOURS_DAYS:
        is_closed = 1 if request.POST.get(f"day_{day}_closed") else 0
        open_time = (request.POST.get(f"day_{day}_open") or "").strip()
        close_time = (request.POST.get(f"day_{day}_close") or "").strip()
        if not is_closed and not (open_time or close_time):
            continue
        hours, _ = PharmacyHours.objects.get_or_create(
            pharmacy=pharmacy, day_of_week=day,
            defaults={"day_name": day_name})
        hours.day_name = day_name
        hours.is_closed = is_closed
        hours.open_time = "" if is_closed else open_time
        hours.close_time = "" if is_closed else close_time
        hours.save()
    _activity(request, "hours_saved", "Updated operating hours")
    return redirect("/pharmacist-dashboard?tab=hours&msg=Hours+saved")


# Fields an admin may edit on the publishable row from /admin-pharmacies.
# Everything else (aggregates, ratings, timestamps) is derived data.
_EDITABLE_PUB_FIELDS = (
    "name", "address", "city", "state", "zip", "phone", "website",
    "is_digital",
)


@admin_required
@require_POST
def edit_pharmacy_publishable(request, pharmacy_id: int):
    """Save an admin edit to a pharmacy's publishable row.

    Edits land on `pharmacies_publishable` (the table the public search reads)
    and set `admin_edited=True`, so the next publish sync won't overwrite the
    curated values with fresh API data. publish_status toggles visibility:
    'published' shows in search, 'unpublished' hides it.
    """
    from ...models import PharmacyPublishable
    row = (
        PharmacyPublishable.objects.filter(pk=pharmacy_id)
        .select_related("pharmacy").first()
    )
    if not row:
        return redirect("/admin-pharmacies?error=Pharmacy+not+found")

    for f in _EDITABLE_PUB_FIELDS:
        if f == "is_digital":
            row.is_digital = 1 if request.POST.get("is_digital") else 0
        else:
            val = (request.POST.get(f) or "").strip()
            setattr(row, f, val)

    publish_status = (request.POST.get("publish_status") or "published").strip()
    row.publish_status = publish_status if publish_status in (
        "published", "unpublished") else "published"
    row.admin_edited = True
    row.save(update_fields=_EDITABLE_PUB_FIELDS + ("publish_status", "admin_edited"))

    # Reflect the soft status change on the source pharmacy too, so the
    # canonical table stays in sync with what the public sees.
    if row.pharmacy:
        row.pharmacy.status = "active" if publish_status == "published" else "deleted"
        row.pharmacy.save(update_fields=["status"])

    _activity(request, "pharmacy_publishable_edit",
              f"{request.user.email} edited publishable #{row.pk} -> {publish_status}")
    return redirect("/admin-pharmacies?success=Saved+edits+for+pharmacy+%23"
                    f"{row.pk}")
