"""Public page views (no auth required)."""
from __future__ import annotations

from django.shortcuts import render

from .. import page_data
from .common import render_page


def index(request):
    return render_page(request, "index.html", page_data.page_index)


def pharmacy_list(request):
    return render_page(request, "list.html", page_data.page_list)


def pharmacy_map(request):
    return render_page(request, "map.html", page_data.page_map)


def online_pharmacies(request):
    return render_page(request, "online.html", page_data.page_online)


def pharmacy_detail(request, pharmacy_id=None, slug=None):
    # Accept both SEO URL form (/pharmacy/<id>/<slug>/) and legacy query form
    # (/pharmacy?id=<id>). The slug is cosmetic; the numeric id is canonical.
    pid = pharmacy_id if pharmacy_id is not None else request.GET.get("id")
    ctx = page_data.page_pharmacy_detail(request, pharmacy_id=pid)
    return render(request, "pharmacypulse/pages/pharmacy.html", ctx)


def reviews(request):
    return render_page(request, "reviews.html", page_data.page_reviews)


def write_review(request):
    return render_page(request, "review.html", page_data.page_write_review)


def compare(request):
    return render_page(request, "compare.html", page_data.page_compare)


def shortages(request):
    return render_page(request, "shortages.html", page_data.page_shortages)


def insights(request):
    return render_page(request, "insights.html", page_data.page_insights)


def for_pharmacies(request):
    return render_page(request, "for-pharmacies.html", page_data.page_for_pharmacies)


from django.views.decorators.clickjacking import xframe_options_exempt  # noqa: E402


@xframe_options_exempt
def widget(request):
    return render_page(request, "widget.html", page_data.page_widget)


def privacy(request):
    return render_page(request, "privacy.html", page_data.page_privacy)


def terms(request):
    return render_page(request, "terms.html", page_data.page_terms)