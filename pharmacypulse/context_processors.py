"""Inject brand + user snapshot into every template.

Localization is handled by Django's built-in i18n machinery (LocaleMiddleware
+ {% trans %}), so this module no longer loads YAML dictionaries.
"""
from __future__ import annotations

from django.conf import settings

from .models.community import SavedComparison


def app_context(request):
    user = getattr(request, "user", None)
    user_snapshot = None
    saved_compare_count = 0
    if user is not None and getattr(user, "is_authenticated", False):
        user_snapshot = {
            "id": user.id,
            "email": user.email,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "role": user.role,
            "phone": user.phone,
            "zip_code": user.zip_code,
        }
        saved_compare_count = SavedComparison.objects.filter(user=user).count()
    return {
        "brand": settings.BRAND,
        "role_scopes": settings.ROLE_SCOPES,
        "user_snapshot": user_snapshot,
        "google_places_key": settings.GOOGLE_PLACES_KEY,
        "saved_compare_count": saved_compare_count,
    }
