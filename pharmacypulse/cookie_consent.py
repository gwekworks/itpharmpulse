from __future__ import annotations

from django.http import HttpResponseBadRequest, HttpResponseRedirect, JsonResponse
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

CONSENT_COOKIE_NAME = "pp_cookie_consent"
CONSENT_VERSION = "v3"
CONSENT_COOKIE_MAX_AGE = 60 * 60 * 24 * 180


def cookie_consent_context(request):
    consent = request.COOKIES.get(CONSENT_COOKIE_NAME, "")
    parts = consent.split(":")
    valid_consent = (
        len(parts) == 4
        and parts[0] == CONSENT_VERSION
        and parts[1] in ("f0", "f1")
        and parts[2] in ("a0", "a1")
        and parts[3] in ("m0", "m1")
    )
    functional_consent = valid_consent and parts[1] == "f1"
    analytics_consent = valid_consent and parts[2] == "a1"
    advertising_preference = valid_consent and parts[3] == "m1"
    return {
        "cookie_consent_set": valid_consent,
        "functional_consent": functional_consent,
        "analytics_consent": analytics_consent,
        "advertising_preference": advertising_preference,
    }


@require_POST
def save_cookie_consent(request):
    choice = request.POST.get("choice")
    if choice == "accept":
        functional_allowed = True
        analytics_allowed = True
        advertising_preference = True
    elif choice == "reject":
        functional_allowed = False
        analytics_allowed = False
        advertising_preference = False
    elif choice == "save":
        functional_value = request.POST.get("functional", "")
        analytics_value = request.POST.get("analytics", "")
        advertising_value = request.POST.get("advertising", "")
        if (
            functional_value not in ("", "1")
            or analytics_value not in ("", "1")
            or advertising_value not in ("", "1")
        ):
            return HttpResponseBadRequest("Invalid cookie preferences.")
        functional_allowed = functional_value == "1"
        analytics_allowed = analytics_value == "1"
        advertising_preference = advertising_value == "1"
    else:
        return HttpResponseBadRequest("Invalid consent choice.")

    next_url = request.POST.get("next", "")
    if not url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        next_url = reverse("index")

    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    response = JsonResponse({
        "ok": True,
        "set": True,
        "functional": functional_allowed,
        "analytics": analytics_allowed,
        "advertising": advertising_preference,
    }) if is_ajax else HttpResponseRedirect(next_url)
    consent_value = (
        f"{CONSENT_VERSION}:f{int(functional_allowed)}:"
        f"a{int(analytics_allowed)}:m{int(advertising_preference)}"
    )
    response.set_cookie(
        CONSENT_COOKIE_NAME,
        consent_value,
        max_age=CONSENT_COOKIE_MAX_AGE,
        httponly=True,
        secure=request.is_secure(),
        samesite="Lax",
        path="/",
    )
    if not functional_allowed:
        response.delete_cookie("lang", path="/", samesite="Lax")
        response.delete_cookie("pp_loc", path="/", samesite="Lax")
    if not analytics_allowed:
        for cookie_name in request.COOKIES:
            if cookie_name.startswith(("_ga", "_gid", "_gat")):
                response.delete_cookie(cookie_name, path="/", samesite="Lax")
    return response
