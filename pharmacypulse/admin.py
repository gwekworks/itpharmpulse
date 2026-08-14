from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import (
    ActivityLog, DataRequest, DrugShortage, ModerationKeyword,
    NewsletterSubscriber, Notification, Pharmacy, PharmacyClaim, PharmacyHours,
    PharmacyOrg, PharmacyPublishable, PharmacyService, PharmacyTeamMember,
    PinnedShortage, ResponseCount, Review, ReviewResponse, SavedComparison,
    User, UserConsent,
)


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    list_display = ("email", "first_name", "last_name", "role", "is_staff", "deactivated_at")
    list_filter = ("role", "is_staff", "is_superuser", "is_active")
    search_fields = ("email", "first_name", "last_name", "phone")
    ordering = ("-date_joined",)
    fieldsets = DjangoUserAdmin.fieldsets + (
        ("PharmacyPulse", {"fields": ("role", "phone", "zip_code", "google_sub", "facebook_id", "deactivated_at")}),
    )


@admin.register(Pharmacy)
class PharmacyAdmin(admin.ModelAdmin):
    list_display = ("name", "city", "state", "zip", "status", "total_reviews", "avg_service_rating", "claimed_by", "listing_completed_at")
    list_filter = ("status", "state", "is_digital")
    search_fields = ("name", "address", "city", "zip", "npi_number")


@admin.register(PharmacyPublishable)
class PharmacyPublishableAdmin(admin.ModelAdmin):
    list_display = ("name", "city", "state", "zip", "publish_status", "admin_edited", "total_reviews")
    list_filter = ("publish_status", "state", "admin_edited")
    search_fields = ("name", "address", "city", "zip", "npi_number")
    readonly_fields = ("pharmacy", "admin_edited")

    def save_model(self, request, obj, form, change):
        # Any admin save/edit marks the row as curator-edited so the publish
        # sync leaves it alone from now on.
        obj.admin_edited = True
        super().save_model(request, obj, form, change)


@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = ("pharmacy_name", "user_name", "service_rating", "stock_available", "moderation_status", "created_at")
    list_filter = ("moderation_status", "stock_available")
    search_fields = ("pharmacy_name", "user_name", "comment")


@admin.register(PharmacyClaim)
class ClaimAdmin(admin.ModelAdmin):
    list_display = ("pharmacy_name", "user_name", "npi_number", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("pharmacy_name", "user_name", "npi_number")


@admin.register(DrugShortage)
class ShortageAdmin(admin.ModelAdmin):
    list_display = ("drug_name", "generic_name", "manufacturer", "status", "updated_at")
    list_filter = ("status",)
    search_fields = ("drug_name", "generic_name", "manufacturer")


for m in (ActivityLog, DataRequest, ModerationKeyword, NewsletterSubscriber,
          Notification, PharmacyHours, PharmacyOrg, PharmacyService,
          PharmacyTeamMember, PinnedShortage, ResponseCount, ReviewResponse,
          SavedComparison, UserConsent):
    admin.site.register(m)
