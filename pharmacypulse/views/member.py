"""Authenticated member page views (login required)."""
from __future__ import annotations

from django.contrib.auth.decorators import login_required

from .. import page_data
from .common import render_page


@login_required
def home(request):
    return render_page(request, "home.html", page_data.page_home)


@login_required
def account(request):
    return render_page(request, "account.html", page_data.page_account)


@login_required
def claim(request):
    return render_page(request, "claim.html", page_data.page_claim)


@login_required
def pharmacist_dashboard(request):
    return render_page(request, "pharmacist-dashboard.html", page_data.page_pharmacist_dashboard)