"""Admin page views (role=admin)."""
from __future__ import annotations

from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache

from .. import page_data
from ..domains.common import set_search_radius_mi
from ..models import PharmacyFact
from .common import admin_required, render_page


@admin_required
def admin_dashboard(request):
    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "set_search_radius":
            set_search_radius_mi(request.POST.get("radius", ""))
            return redirect("/admin-dashboard")
    return render_page(request, "admin-dashboard.html", page_data.page_admin_dashboard)


@admin_required
def admin_analytics(request):
    return render_page(request, "admin-analytics.html", page_data.page_admin_analytics)


@admin_required
def admin_claims(request):
    return render_page(request, "admin-claims.html", page_data.page_admin_claims)


@admin_required
def admin_moderation(request):
    return render_page(request, "admin-moderation.html", page_data.page_admin_moderation)


@admin_required
@never_cache
def admin_pharmacies(request):
    return render_page(request, "admin-pharmacies.html", page_data.page_admin_pharmacies)


@admin_required
def admin_shortages(request):
    return render_page(request, "admin-shortages.html", page_data.page_admin_shortages)


@admin_required
def admin_users(request):
    return render_page(request, "admin-users.html", page_data.page_admin_users)


@admin_required
def admin_pharmacy_facts(request):
    """Apr-16 sync action: edit homepage 'Did you know' fact cards from admin."""
    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "update":
            for fact in PharmacyFact.objects.all():
                pid = str(fact.id)
                fact.icon = request.POST.get(f"icon_{pid}", fact.icon)
                fact.stat = request.POST.get(f"stat_{pid}", fact.stat)
                fact.head = request.POST.get(f"head_{pid}", fact.head)
                fact.body = request.POST.get(f"body_{pid}", fact.body)
                fact.cta = request.POST.get(f"cta_{pid}", fact.cta)
                fact.src = request.POST.get(f"src_{pid}", fact.src)
                fact.cta_link = request.POST.get(f"cta_link_{pid}", fact.cta_link)
                fact.sort_order = int(request.POST.get(f"sort_{pid}", fact.sort_order) or 0)
                fact.is_active = request.POST.get(f"active_{pid}") == "on"
                fact.save()
        elif action == "create":
            PharmacyFact.objects.create(
                icon=request.POST.get("new_icon", ""),
                stat=request.POST.get("new_stat", ""),
                head=request.POST.get("new_head", ""),
                body=request.POST.get("new_body", ""),
                cta=request.POST.get("new_cta", ""),
                src=request.POST.get("new_src", ""),
                cta_link=request.POST.get("new_cta_link", ""),
                sort_order=int(request.POST.get("new_sort", 0) or 0),
                is_active=request.POST.get("new_active") == "on",
            )
        elif action == "delete":
            PharmacyFact.objects.filter(id=request.POST.get("id")).delete()
        return redirect("/admin-pharmacy-facts")
    return render(
        request, "pharmacypulse/pages/admin-pharmacy-facts.html",
        {"facts": PharmacyFact.objects.all()},
    )