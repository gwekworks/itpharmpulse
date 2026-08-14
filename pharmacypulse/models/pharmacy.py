"""Pharmacy models: Pharmacy + its searchable publishable copy, hours, services, ZIP centroid."""
from django.db import models


class Pharmacy(models.Model):
    name = models.TextField()
    address = models.TextField()
    city = models.TextField(default="Philadelphia")
    state = models.TextField(default="PA")
    zip = models.TextField()
    phone = models.TextField(blank=True, null=True)
    latitude = models.FloatField(blank=True, null=True)
    longitude = models.FloatField(blank=True, null=True)
    stock_confidence = models.IntegerField(default=0)
    avg_wait_time = models.IntegerField(default=0)
    avg_service_rating = models.FloatField(default=0)
    total_reviews = models.IntegerField(default=0)
    is_digital = models.IntegerField(default=0)
    website = models.TextField(blank=True, null=True)
    # NPI = National Provider Identifier (10 digits) from NPPES. The authoritative
    # ID for any US-licensed pharmacy. Used as the dedup natural key.
    # Indexed because sync flows look up by NPI.
    npi_number = models.TextField(blank=True, null=True, db_index=True)
    # Google Places place_id — separate from NPI, used to enrich the row with
    # hours/lat-lng/photos. May be empty until enrichment runs.
    place_id = models.TextField(blank=True, null=True, db_index=True)
    # NPPES taxonomy code (3336C0003X = Community/Retail, 3336M0002X = Mail Order,
    # 3336S0011X = Specialty, 3336C0004X = Compounding). Drives is_digital + display.
    taxonomy_code = models.CharField(max_length=16, blank=True, default="")
    # When this NPI was first issued by CMS — proxy for "in business since".
    # Patients see this as 'Established YYYY' on the detail page.
    enumeration_date = models.DateField(blank=True, null=True)
    # Authorized official from NPPES — owner, pharmacist-in-charge, etc.
    # 'Owned by Jane Doe' on the detail page humanizes independent pharmacies.
    authorized_official_name = models.TextField(blank=True, default="")
    authorized_official_title = models.TextField(blank=True, default="")
    # Comma-separated secondary taxonomy codes — e.g. a retail pharmacy that
    # also offers compounding has 3336C0003X primary + 3336C0004X here. Drives
    # 'Compounding', 'Specialty', 'Mail Order' service badges.
    secondary_taxonomies = models.TextField(blank=True, default="")
    # Set every time we attempt Google Places lazy enrichment, regardless of
    # whether it succeeded. Read by enrich.needs_enrichment to enforce a
    # 6-month cooldown — without this, pharmacies that Google can't find
    # would re-trigger enrichment on every pageview, burning API quota.
    enrichment_checked_at = models.DateTimeField(blank=True, null=True)
    # Number of pharmacies sharing this DBA name. Computed via SQL aggregate
    # after each NPPES sync. chain_size >= 5 is treated as a chain in the UI;
    # everything else is independent. The threshold is heuristic — small
    # franchises (e.g. 'MEDICAP PHARMACY' with 121 locations) are correctly
    # caught, while a unique 'BOB'S CORNER PHARMACY' stays independent.
    chain_size = models.IntegerField(default=1, db_index=True)
    claimed_by = models.IntegerField(blank=True, null=True)
    org = models.ForeignKey(
        "PharmacyOrg", null=True, blank=True, on_delete=models.SET_NULL, db_column="org_id",
        related_name="pharmacies",
    )
    status = models.TextField(default="active")
    phone_wait_time = models.IntegerField(blank=True, null=True)
    delivery_wait_time = models.IntegerField(blank=True, null=True)
    delivery_rating = models.FloatField(default=0)
    customer_service_hours = models.TextField(blank=True, null=True)
    # First time the owner's "Complete your listing" checklist hit 100%.
    # Drives the one-time confetti celebration and the persistent Verified
    # badge shown next to this pharmacy's name sitewide.
    listing_completed_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        db_table = "pharmacies"


class PharmacyPublishable(models.Model):
    """Searchable snapshot of the pharmacies table.

    The public search/browse pages query this table instead of `pharmacies`
    so we can gate what the public sees via `publish_status` without touching
    the live API-synced data. Rebuilt after every pharmacy API sync.

    `pharmacy` is a OneToOne pointing at the source row and doubles as the
    primary key, so a publishable row's `id` always equals the source
    pharmacy's id — detail URLs (/pharmacy/{id}/{slug}/) keep working.

    publish_status:
      - 'published'  → appears in public search (default)
      - 'unpublished' → hidden from public search; e.g. manually removed,
        source pharmacy deleted, or awaiting review.
    """
    pharmacy = models.OneToOneField(
        "Pharmacy", on_delete=models.CASCADE, db_column="pharmacy_id",
        primary_key=True, related_name="publishable",
    )
    publish_status = models.TextField(default="published", db_index=True)
    # True once an admin saves/edits this row in Django admin. The publish
    # sync never overwrites rows where this is True, so a curator's edits
    # (custom name, publish_status, etc.) are preserved.
    admin_edited = models.BooleanField(default=False)

    # ---- Mirror of the display/searchable columns on `pharmacies` ----
    name = models.TextField()
    address = models.TextField()
    city = models.TextField(default="Philadelphia")
    state = models.TextField(default="PA")
    zip = models.TextField()
    phone = models.TextField(blank=True, null=True)
    latitude = models.FloatField(blank=True, null=True)
    longitude = models.FloatField(blank=True, null=True)
    stock_confidence = models.IntegerField(default=0)
    avg_wait_time = models.IntegerField(default=0)
    avg_service_rating = models.FloatField(default=0)
    total_reviews = models.IntegerField(default=0)
    is_digital = models.IntegerField(default=0)
    website = models.TextField(blank=True, null=True)
    npi_number = models.TextField(blank=True, null=True, db_index=True)
    place_id = models.TextField(blank=True, null=True, db_index=True)
    taxonomy_code = models.CharField(max_length=16, blank=True, default="")
    enumeration_date = models.DateField(blank=True, null=True)
    authorized_official_name = models.TextField(blank=True, default="")
    authorized_official_title = models.TextField(blank=True, default="")
    secondary_taxonomies = models.TextField(blank=True, default="")
    chain_size = models.IntegerField(default=1, db_index=True)
    claimed_by = models.IntegerField(blank=True, null=True)
    org_id = models.IntegerField(blank=True, null=True)
    status = models.TextField(default="active")
    phone_wait_time = models.IntegerField(blank=True, null=True)
    delivery_wait_time = models.IntegerField(blank=True, null=True)
    delivery_rating = models.FloatField(default=0)
    customer_service_hours = models.TextField(blank=True, null=True)
    listing_completed_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "pharmacies_publishable"
        ordering = ["-total_reviews"]

    @property
    def id(self):
        # PK field is the OneToOne `pharmacy`, so expose the source pharmacy
        # id under `id` too — keeps _pharmacy_dict and detail URLs stable.
        return self.pk


class PharmacyHours(models.Model):
    pharmacy = models.ForeignKey(
        "Pharmacy", null=True, blank=True, on_delete=models.CASCADE, db_column="pharmacy_id",
        related_name="hours",
    )
    day_of_week = models.IntegerField()
    day_name = models.TextField()
    open_time = models.TextField(blank=True, null=True)
    close_time = models.TextField(blank=True, null=True)
    is_closed = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "pharmacy_hours"


class PharmacyService(models.Model):
    pharmacy = models.ForeignKey(
        "Pharmacy", null=True, blank=True, on_delete=models.CASCADE, db_column="pharmacy_id",
        related_name="services",
    )
    service_type = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "pharmacy_services"


class PharmacyCoveragePlan(models.Model):
    """Coverage plans a pharmacy accepts (e.g. Medicare Part D), shown to
    patients on the public listing and managed from the pharmacist dashboard."""
    pharmacy = models.ForeignKey(
        "Pharmacy", null=True, blank=True, on_delete=models.CASCADE, db_column="pharmacy_id",
        related_name="coverage_plans",
    )
    plan_name = models.TextField()
    plan_type = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "pharmacy_coverage_plans"


class PharmacyInsuranceProvider(models.Model):
    """Insurance companies / PBMs a pharmacy is contracted with, shown to
    patients on the public listing and managed from the pharmacist dashboard."""
    pharmacy = models.ForeignKey(
        "Pharmacy", null=True, blank=True, on_delete=models.CASCADE, db_column="pharmacy_id",
        related_name="insurance_providers",
    )
    provider_name = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "pharmacy_insurance_providers"


class ZipCentroid(models.Model):
    """Cache of ZIP code → approximate lat/lng centroid. Lets us put map
    pins on every pharmacy by ZIP without paying for street-level Google
    Place lookups (lazy enrichment refines that on detail-page view).

    One row per unique US ZIP. ~5K rows in our data, capped at ~42K (every
    US ZIP). Resolved lazily by management command via Google Geocoding API
    (~$0.005/request, $25 one-time cost vs. ~$1,200 for per-pharmacy Find Place
    + Place Details lookups for the same map-pin coverage)."""
    zip = models.CharField(max_length=5, unique=True, primary_key=True)
    latitude = models.FloatField()
    longitude = models.FloatField()
    city = models.CharField(max_length=64, blank=True, default="")
    state = models.CharField(max_length=2, blank=True, default="")
    resolved_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "zip_centroids"