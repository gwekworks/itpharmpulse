from django.contrib import admin
from django.urls import include, path

# SECURITY TODO (admin hardening):
#   1. Require 2FA / TOTP for staff logins before exposing this panel (e.g.
#      django-otp or django-allauth-2fa).
#   2. Optionally IP-allow-list the admin paths in production via middleware.
#   3. Keep the non-default path below; never use /admin/.
urlpatterns = [
    path("django-admin/", admin.site.urls),
    path("i18n/", include("django.conf.urls.i18n")),
    path("", include("pharmacypulse.urls")),
]
