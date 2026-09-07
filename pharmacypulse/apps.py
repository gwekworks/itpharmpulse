from django.apps import AppConfig


class PharmacyPulseConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "pharmacypulse"
    verbose_name = "PharmacyPulse"

    def ready(self):
        from . import signals  # noqa: F401
