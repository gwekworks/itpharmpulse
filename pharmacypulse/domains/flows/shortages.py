"""FDA drug-shortage sync + manual add."""
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
    Pharmacy, PharmacyClaim, PharmacyHours, PharmacyOrg, PharmacyTeamMember,
    ResponseCount, Review, ReviewResponse, SavedComparison, User, UserConsent,
)

from .common import admin_required, _activity


@admin_required
def sync_fda_shortages(request):
    inserted = updated = 0
    for status_filter, stored in (("Current", "current"), ("Resolved", "resolved")):
        try:
            r = requests.get(
                "https://api.fda.gov/drug/shortages.json",
                params={"search": f"status:{status_filter}", "limit": 100 if stored == "current" else 50},
                timeout=30,
            )
            if not r.ok:
                continue
            for s in (r.json() or {}).get("results", []) or []:
                name = s.get("generic_name") or ""
                manuf = s.get("company_name") or ""
                if not name:
                    continue
                related_info = (s.get("related_info") or "").strip()
                availability = (s.get("availability") or "").strip()
                update_date = (s.get("update_date") or "").strip()
                _, created = DrugShortage.objects.update_or_create(
                    drug_name=name, manufacturer=manuf,
                    defaults={
                        "generic_name": name, "status": stored,
                        "reason": related_info[:500],
                        "alternatives": availability[:500],
                        "estimated_resolution": update_date,
                    },
                )
                if created:
                    inserted += 1
                else:
                    updated += 1
        except requests.RequestException:
            continue
    _activity(request, "shortage_sync", f"FDA sync: {inserted} new, {updated} updated")
    return redirect(f"/admin-shortages?success=Synced+{inserted}+new+{updated}+updated")


@admin_required
def add_shortage(request):
    drug_name = (request.POST.get("drug_name") or "").strip()
    if not drug_name:
        return redirect("/admin-shortages?error=Drug+name+required")
    DrugShortage.objects.create(
        drug_name=drug_name,
        generic_name=request.POST.get("generic_name") or "",
        manufacturer=request.POST.get("manufacturer") or "",
        status=request.POST.get("shortage_status") or "current",
        reason=request.POST.get("reason") or "",
        estimated_resolution=request.POST.get("estimated_resolution") or "",
        alternatives=request.POST.get("alternatives") or "",
    )
    _activity(request, "shortage_added", f"Added shortage: {drug_name}")
    return redirect("/admin-shortages?success=1")
