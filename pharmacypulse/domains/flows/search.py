"""/api/flow/search autocomplete endpoint."""
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
from ...domains.common import _zip_city_state


def search_pharmacies(request):
    from django.db.models import Q as _Q
    from ...domains.common import published_pharmacies
    q = (request.GET.get("q") or "").strip()
    qs = published_pharmacies()
    if q:
        # Review picker + search autocomplete both call with a query string.
        zip_q = re.sub(r"\D", "", q)[:5]
        q_filter = _Q(name__icontains=q) | _Q(city__icontains=q) | _Q(address__icontains=q)
        if len(zip_q) >= 3:
            q_filter |= _Q(zip__startswith=zip_q)
        if len(zip_q) == 5:
            # Pharmacies have no ZIP → resolve the ZIP to its location name
            # and match pharmacies within that location.
            zip_city = _zip_city_state(zip_q)
            if zip_city:
                city_name, state_code = zip_city
                q_filter |= _Q(city__iexact=city_name, state__iexact=state_code)
        qs = qs.filter(q_filter)
    elif request.GET.get("popular") == "1":
        # Review picker seeds its list with the most-reviewed pharmacies when
        # no query has been typed yet. No other caller omits `q`.
        qs = qs.filter(total_reviews__gt=0)
    else:
        return JsonResponse({"results": []})
    qs = qs.order_by("-total_reviews", "name")[:8]
    from ...text_utils import pharmacy_slug
    results = [{
        "id": p.id, "name": p.name, "address": p.address, "city": p.city,
        "state": p.state, "zip": p.zip, "phone": p.phone,
        "avg_service_rating": p.avg_service_rating,
        "total_reviews": p.total_reviews, "is_digital": p.is_digital,
        "slug": pharmacy_slug(p.name),
    } for p in qs]
    return JsonResponse({"results": results})
