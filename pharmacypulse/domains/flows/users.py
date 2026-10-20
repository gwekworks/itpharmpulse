"""User admin, CCPA, profile/password, team membership."""
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

from .common import admin_required, _activity, safe_next


@admin_required
def suspend_user(request, target_user_id: int):
    u = User.objects.filter(id=target_user_id).first()
    if not u:
        return redirect("/admin-users?error=User+not+found")
    if u == request.user:
        return redirect("/admin-users?error=Cannot+suspend+yourself")
    User.objects.filter(id=target_user_id).update(deactivated_at=djtz.now())
    _activity(request, "user_suspended", f"Suspended {u.email}")
    return redirect(safe_next(request, "/admin-users?success=User+suspended"))


@admin_required
def unsuspend_user(request, target_user_id: int):
    u = User.objects.filter(id=target_user_id).first()
    if not u:
        return redirect("/admin-users?error=User+not+found")
    User.objects.filter(id=target_user_id).update(deactivated_at=None)
    _activity(request, "user_unsuspended", f"Unsuspended {u.email}")
    return redirect(safe_next(request, "/admin-users?success=User+unsuspended"))


@admin_required
def change_user_role(request, target_user_id: int):
    role = (request.POST.get("role") or "").strip()
    if role not in ("user", "pharmacist", "admin"):
        return redirect("/admin-users?error=Invalid+role")
    u = User.objects.filter(id=target_user_id).first()
    if not u:
        return redirect("/admin-users?error=User+not+found")
    if u == request.user and role != "admin":
        return redirect("/admin-users?error=Cannot+demote+yourself")
    old_role = u.role
    u.role = role
    u.save(update_fields=["role"])
    _activity(request, "role_change", f"Changed {u.email} from {old_role} → {role}")
    return redirect(safe_next(request, "/admin-users?success=Role+updated"))


@login_required
def ccpa_export(request):
    u = request.user
    reviews = list(Review.objects.filter(user=u, deleted_at__isnull=True).values(
        "id", "pharmacy_name", "stock_available", "wait_time_rating", "service_rating",
        "comment", "is_caregiver", "created_at",
    ))
    return JsonResponse({
        "profile": [{
            "id": u.id, "phone": u.phone, "first_name": u.first_name,
            "last_name": u.last_name, "role": u.role,
            "created_at": u.date_joined.isoformat() if u.date_joined else None,
        }],
        "reviews": [dict(r, created_at=r["created_at"].isoformat() if r["created_at"] else None) for r in reviews],
    })


@login_required
def ccpa_delete(request):
    u = request.user
    now = djtz.now()
    DataRequest.objects.create(
        user=u, request_type="deletion", status="processing",
        due_date=now + timedelta(days=45),
    )
    Review.objects.filter(user=u).update(user_name="Deleted User", comment=None, deleted_at=now)
    PharmacyClaim.objects.filter(user=u).update(user_name="Deleted User")
    UserConsent.objects.filter(user=u, revoked_at__isnull=True).update(revoked_at=now)
    _activity(request, "ccpa_delete", "Account deletion requested and data anonymized")
    DataRequest.objects.filter(user=u, request_type="deletion", status="processing").update(
        status="completed", completed_at=now,
    )
    # Scrub PII on the user row itself. Email is unique-constrained so we
    # rotate it to a non-routable sentinel scoped by the original user id —
    # keeps the row but breaks any link back to the human. The .invalid
    # TLD is reserved for sentinel values per RFC 2606 and never routes.
    import secrets
    sentinel = f"deleted-{u.id}-{secrets.token_hex(4)}@deleted.invalid"
    User.objects.filter(id=u.id).update(
        deactivated_at=now,
        email=sentinel, username=sentinel,
        first_name="Deleted", last_name="User",
        phone="", zip_code="", google_sub="",
    )
    from django.contrib.auth import logout
    logout(request)
    return redirect("/login?success=Your+account+has+been+deleted+per+CCPA+requirements")


@login_required
def update_profile(request):
    """Update first/last name, phone, and zip from /account inline form."""
    u = request.user
    first = (request.POST.get("first_name") or "").strip()[:150]
    last  = (request.POST.get("last_name")  or "").strip()[:150]
    phone = (request.POST.get("phone")      or "").strip()[:32]
    zip_  = (request.POST.get("zip_code")   or "").strip()[:16]
    if first:
        u.first_name = first
    u.last_name = last
    u.phone = phone
    u.zip_code = zip_
    u.save(update_fields=["first_name", "last_name", "phone", "zip_code"])
    return redirect("/account?updated=profile")


@login_required
def change_password(request):
    """Verify current password, then set a new one. Keeps the user signed in."""
    from django.contrib.auth import update_session_auth_hash
    from django.contrib.auth.password_validation import validate_password
    from django.core.exceptions import ValidationError as _VE
    u = request.user
    current = request.POST.get("current_password", "")
    new     = request.POST.get("new_password", "")
    confirm = request.POST.get("confirm_password", "")
    if not u.check_password(current):
        return redirect("/account?pw_error=current")
    if new != confirm:
        return redirect("/account?pw_error=match")
    try:
        validate_password(new, user=u)
    except _VE as exc:
        return redirect(f"/account?pw_error=weak&pw_msg={urllib.parse.quote(' '.join(exc.messages))}")
    u.set_password(new)
    u.save(update_fields=["password"])
    update_session_auth_hash(request, u)
    return redirect("/account?updated=password")


@login_required
def invite_team_member(request):
    u = request.user
    my_tm = (PharmacyTeamMember.objects.filter(user=u, role="owner")
             .select_related("org").first())
    if not my_tm:
        return redirect("/pharmacist-dashboard?tab=team&error=You+must+be+an+org+owner+to+invite+members")
    org = my_tm.org
    invite_email = (request.POST.get("invite_email") or "").strip().lower()
    invite_role = (request.POST.get("invite_role") or "member").strip()
    if not invite_email:
        return redirect("/pharmacist-dashboard?tab=team&error=Email+required")
    if PharmacyTeamMember.objects.filter(org=org, email__iexact=invite_email).exists():
        return redirect("/pharmacist-dashboard?tab=team&error=This+person+is+already+a+member+or+has+a+pending+invite")
    token = secrets.token_hex(16)
    PharmacyTeamMember.objects.create(
        org=org, email=invite_email, role=invite_role,
        invite_token=token, invited_by=u.id,
    )
    _activity(request, "team_invite", f"Invited {invite_email} to {org.name}")
    return redirect(f"/pharmacist-dashboard?tab=team&msg=Invite+sent+to+{urllib.parse.quote(invite_email)}")


@login_required
def accept_team_invite(request):
    token = (request.GET.get("token") or "").strip()
    invite = PharmacyTeamMember.objects.filter(
        invite_token=token, accepted_at__isnull=True,
    ).first()
    if not invite:
        return redirect("/home?error=Invalid+or+expired+invite+link")
    # SECURITY: bind the invite to the email it was issued for. A forwarded
    # invite link must not let a different account join the org.
    if request.user.email.lower() != invite.email.lower():
        return redirect("/home?error=This+invite+is+for+a+different+account")
    invite.user = request.user
    invite.accepted_at = djtz.now()
    invite.invite_token = None
    invite.save()
    if request.user.role != "admin":
        User.objects.filter(id=request.user.id).update(role="pharmacist")
    _activity(request, "team_joined", f"Accepted invite to org #{invite.org_id}")
    return redirect("/pharmacist-dashboard?welcome=1")


@login_required
def remove_team_member(request, member_id: int):
    m = (PharmacyTeamMember.objects.filter(id=member_id)
         .select_related("org").first())
    if not m:
        return redirect("/pharmacist-dashboard?tab=team&error=Member+not+found")
    if m.org.owner_id != request.user.id:
        return redirect("/pharmacist-dashboard?tab=team&error=Only+the+org+owner+can+remove+members")
    if m.user_id == request.user.id:
        return redirect("/pharmacist-dashboard?tab=team&error=You+cannot+remove+yourself+as+the+owner")
    email = m.email
    m.delete()
    _activity(request, "team_removed", f"Removed {email} from org")
    return redirect("/pharmacist-dashboard?tab=team&msg=Member+removed")


@login_required
def update_team_role(request, member_id: int):
    m = (PharmacyTeamMember.objects.filter(id=member_id)
         .select_related("org").first())
    if not m:
        return redirect("/pharmacist-dashboard?tab=team&error=Member+not+found")
    if m.org.owner_id != request.user.id:
        return redirect("/pharmacist-dashboard?tab=team&error=Only+the+org+owner+can+change+roles")
    new_role = (request.POST.get("new_role") or "member").strip()
    PharmacyTeamMember.objects.filter(id=member_id).update(role=new_role)
    return redirect("/pharmacist-dashboard?tab=team&msg=Role+updated")
