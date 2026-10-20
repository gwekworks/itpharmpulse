from django.db import migrations


class PgIndex(migrations.RunSQL):
    """RunSQL that only executes on Postgres.

    CREATE INDEX CONCURRENTLY is Postgres-specific. Prod runs on Postgres so
    the indexes still get built there; SQLite (local dev / the test runner)
    just records the migration without erroring.
    """
    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        if schema_editor.connection.vendor == "postgresql":
            super().database_forwards(app_label, schema_editor, from_state, to_state)

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        if schema_editor.connection.vendor == "postgresql":
            super().database_backwards(app_label, schema_editor, from_state, to_state)


class Migration(migrations.Migration):
    """Partial indexes for the most hit read paths discovered during the
    search-performance pass.

    All additive partial indexes on a read-heavy site; CONCURRENTLY avoids
    locking the tables during the build so it can run on a live dyno. That
    requires atomic=False (operations outside a transaction), same pattern as
    0009_pharmacy_active_indexes.

    Covers:
      - /list auto-location + zip searches (pharmacies.zip)
      - /list 'most reviewed' + /map ordering (pharmacies.total_reviews)
      - /list default sort (avg_service_rating → total_reviews → id)
      - pharmacy detail + /reviews read the approved, non-deleted review set
      - /shortages by (status, updated_at)
      - admin claims by (status, created_at)
    """

    atomic = False

    dependencies = [
        ("pharmacypulse", "0009_pharmacy_active_indexes"),
    ]

    operations = [
        PgIndex(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "pharmacies_active_zip_idx ON pharmacies (zip) "
            "WHERE status = 'active' AND deleted_at IS NULL;",
            "DROP INDEX IF EXISTS pharmacies_active_zip_idx;",
        ),
        PgIndex(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "pharmacies_active_total_reviews_idx ON pharmacies (total_reviews) "
            "WHERE status = 'active' AND deleted_at IS NULL;",
            "DROP INDEX IF EXISTS pharmacies_active_total_reviews_idx;",
        ),
        PgIndex(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "pharmacies_active_list_sort_idx "
            "ON pharmacies (avg_service_rating DESC, total_reviews DESC, id) "
            "WHERE status = 'active' AND deleted_at IS NULL;",
            "DROP INDEX IF EXISTS pharmacies_active_list_sort_idx;",
        ),
        PgIndex(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "reviews_approved_pharmacy_idx "
            "ON reviews (pharmacy_id, created_at) "
            "WHERE moderation_status = 'approved' AND deleted_at IS NULL;",
            "DROP INDEX IF EXISTS reviews_approved_pharmacy_idx;",
        ),
        PgIndex(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "reviews_approved_created_idx ON reviews (created_at) "
            "WHERE moderation_status = 'approved' AND deleted_at IS NULL;",
            "DROP INDEX IF EXISTS reviews_approved_created_idx;",
        ),
        PgIndex(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "drug_shortages_status_updated_idx "
            "ON drug_shortages (status, updated_at DESC);",
            "DROP INDEX IF EXISTS drug_shortages_status_updated_idx;",
        ),
        PgIndex(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "pharmacy_claims_status_created_idx "
            "ON pharmacy_claims (status, created_at DESC);",
            "DROP INDEX IF EXISTS pharmacy_claims_status_created_idx;",
        ),
    ]