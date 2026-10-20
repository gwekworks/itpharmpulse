from django.db import migrations


class PgIndex(migrations.RunSQL):
    """RunSQL that only executes on Postgres.

    CREATE INDEX CONCURRENTLY is Postgres-specific. Prod runs on Postgres so
    the index still gets built there; SQLite (local/tests) just records the
    migration without erroring.
    """
    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        if schema_editor.connection.vendor == "postgresql":
            super().database_forwards(app_label, schema_editor, from_state, to_state)

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        if schema_editor.connection.vendor == "postgresql":
            super().database_backwards(app_label, schema_editor, from_state, to_state)


class Migration(migrations.Migration):
    """Partial indexes for the homepage hot path.

    The home view filters Pharmacy by status='active' AND deleted_at IS NULL on
    every request, then splits by is_digital and (when geolocation is known)
    prefilters by a lat/lng bounding box. Without these indexes the planner
    sequential-scans all 79k+ rows per filter.

    CONCURRENTLY avoids locking the table during the build, so the migration
    can run on a live dyno; that requires atomic=False and operations outside
    a transaction.
    """

    atomic = False

    dependencies = [
        ("pharmacypulse", "0008_zipcentroid"),
    ]

    operations = [
        PgIndex(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "pharmacies_active_is_digital_idx "
            "ON pharmacies (is_digital) "
            "WHERE status = 'active' AND deleted_at IS NULL;",
            "DROP INDEX IF EXISTS pharmacies_active_is_digital_idx;",
        ),
        PgIndex(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "pharmacies_active_geo_idx "
            "ON pharmacies (latitude, longitude) "
            "WHERE status = 'active' AND deleted_at IS NULL "
            "AND latitude IS NOT NULL AND longitude IS NOT NULL;",
            "DROP INDEX IF EXISTS pharmacies_active_geo_idx;",
        ),
    ]
