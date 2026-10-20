"""Clear NPPES HIE/XDR/FHIR endpoint URLs that were mistakenly stored as
pharmacy.website. NPPES "endpoints" are clinical data-exchange targets —
NOT consumer pharmacy websites — and ~88% of what got synced was these.

Patterns we strip:
  - bare-IP URLs (https://199.119.81.30:...)
  - /Gateway/, /NhinService/, XDR*_Service, DocumentSubmission
  - /FHIR/, fhirproxy, FHIR-proxy
  - :8291 (NHIN gateway port), :8443/services, /soap/, /wsdl
  - direct.* / directmessaging / directtrust

Real consumer URLs (http://abingtonpharmacy.com/, etc.) stay untouched.

Usage:
    python manage.py clear_bogus_websites          # dry run
    python manage.py clear_bogus_websites --apply  # actually clear
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db.models import Q

from pharmacypulse.models import Pharmacy


# Mirrors the filter in sync_npi_pharmacies._is_consumer_website. Patterns are
# all lowercased and compared against website field (which Django icontains
# already lowercases).
_BAD_SUBSTRINGS = (
    "/gateway/", "/nhinservice", "xdrrequest_service", "xdrresponse_service",
    "documentsubmission", "/fhir/", "fhirproxy", "fhir-proxy",
    ":8291", ":8443/services", "/soap/", "/wsdl",
    "/direct.", "directmessaging", "directtrust",
)


class Command(BaseCommand):
    help = "Clear NPPES HIE/XDR/FHIR endpoint URLs from pharmacy.website."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Actually clear (without this, just counts)")

    def handle(self, *args, **options):
        bad_q = Q()
        for s in _BAD_SUBSTRINGS:
            bad_q |= Q(website__icontains=s)
        # Bare-IP URLs (Postgres-only regex; SQLite local will use .iregex too)
        bad_q |= Q(website__iregex=r"^https?://[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+")

        qs = Pharmacy.objects.filter(bad_q).exclude(website="")
        n = qs.count()
        self.stdout.write(self.style.WARNING(
            f"Found {n} pharmacies with HIE/XDR/FHIR/IP website URLs."
        ))
        for w in qs.values_list("website", flat=True).distinct()[:8]:
            self.stdout.write(f"  e.g. {w}")
        if not options["apply"]:
            self.stdout.write(self.style.NOTICE(
                "\nDry run. Re-run with --apply to clear these to empty."
            ))
            return
        cleared = qs.update(website="")
        self.stdout.write(self.style.SUCCESS(f"Cleared {cleared} bogus website URLs."))
