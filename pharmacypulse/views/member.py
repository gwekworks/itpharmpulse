"""Authenticated member page views (login required)."""
from __future__ import annotations

from django.contrib.auth.decorators import login_required

from .. import page_data
from .common import render_page


@login_required
def home(request):
    return render_page(request, "home.html", page_data.page_home)


@login_required
def saved(request):
    return render_page(request, "saved.html", page_data.page_saved)


@login_required
def account(request):
    return render_page(request, "account.html", page_data.page_account)


@login_required
def claim(request):
    return render_page(request, "claim.html", page_data.page_claim)


@login_required
def transfer(request):
    # Patient-only page: owners/admins are redirected with an error by the
    # flow, but still allow rendering so the message displays.
    return render_page(request, "transfer.html", page_data.page_transfer)


@login_required
def transfers(request):
    # Standalone tracking page for the patient's transfer requests.
    return render_page(request, "transfers.html", page_data.page_transfers)


@login_required
def pharmacist_dashboard(request):
    return render_page(request, "pharmacist-dashboard.html", page_data.page_pharmacist_dashboard)