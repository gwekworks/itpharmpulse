"""Flow endpoints behind /api/flow/... — ports BEN-156-pharmacypulse/flows.yaml 1:1.

Facade over the flow domain modules under :mod:`pharmacypulse.domains.flows`.
Kept as the single import surface so urls.py and management commands don't
have to know the new layout.

External dependencies (key pulled from settings; missing key => friendly error
surface, no crash):
  - FDA:            api.fda.gov (no key)
  - Google Places:  maps.googleapis.com (GOOGLE_PLACES_KEY)
  - NPI Registry:   npiregistry.cms.hhs.gov (no key)
  - Stripe:         api.stripe.com (STRIPE_SECRET_KEY)
"""
from __future__ import annotations

from .domains.flows.shortages import sync_fda_shortages, add_shortage
from .domains.flows.ingest import (
    ingest_pharmacies_by_zip, ingest_zip, fetch_pharmacy_details,
    _is_excluded_pharmacy,
)
from .domains.flows.claims import (
    verify_and_submit_claim, approve_claim, reject_claim,
)
from .domains.flows.reviews import (
    submit_review, respond_to_review, moderate_review, flag_review,
)
from .domains.flows.pharmacies import (
    bulk_delete_pharmacies, bulk_delete_chain, report_pharmacy_data,
    flag_as_closed, dismiss_closed_flag, delete_flagged_pharmacy,
    pharmacy_services_add, pharmacy_services_remove,
    coverage_plan_add, coverage_plan_remove,
    insurance_provider_add, insurance_provider_remove, insurance_plans_add,
    pharmacy_hours_save,
    add_to_compare, remove_from_compare, clear_compare,
    edit_pharmacy_publishable, bulk_publish_pharmacies,
)
from .domains.flows.users import (
    suspend_user, unsuspend_user, change_user_role, ccpa_export,
    ccpa_delete, update_profile, change_password, invite_team_member,
    accept_team_invite, remove_team_member, update_team_role,
)
from .domains.flows.billing import create_checkout, billing_portal, stripe_webhook
from .domains.flows.newsletter import (
    newsletter_subscribe, newsletter_unsubscribe, newsletter_unsubscribe_public,
    make_unsubscribe_url,
)
from .domains.flows.search import search_pharmacies, suggest_locations, place_details
