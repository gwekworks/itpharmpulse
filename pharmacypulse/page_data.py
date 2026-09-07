"""Per-page data providers.

Facade over the domain modules under :mod:`pharmacypulse.domains`. Kept as
the single import surface so views.py and management commands don't have to
know the new layout. Each ``page_*`` function is a thin data provider that
returns a dict handed straight to the template context.

Each function is decorated with @with_get_params, which exposes every key in
request.GET as `{{param_X}}` in the template (e.g. `?status=current` →
`{{param_status}}`).

Stat tiles are returned as 1-element lists like `[{"n": 42}]` so templates
can read `{{alias.0.n}}` — a holdover from the original prod template
shapes. Single-row results follow the same convention.
"""
from __future__ import annotations

from .domains.common import (
    Row, with_get_params, _safe_int, _pharmacy_dict, _review_dict,
    _shortage_dict, _compare_winners, _normalize_validation_error,
    _TAXONOMY_BADGE, _TAXONOMY_LABEL, _STATE_NAMES, BRAND_PATTERNS,
    BRAND_PATTERNS_BY_KEY, DEFUNCT_CHAIN_KEYS,
)
from .domains.home import (
    page_index, page_home,
    _INDEX_AGG_CACHE_KEY, _INDEX_AGG_TTL, _index_aggregates,
    _index_cards, _pharmacy_facts_json,
)
from .domains.pharmacies import (
    page_list, page_map, page_online, page_pharmacy_detail,
    page_compare, page_claim, page_widget,
)
from .domains.reviews import (
    page_reviews, page_write_review, page_for_pharmacies,
)
from .domains.shortages import page_shortages
from .domains.insights import page_insights
from .domains.account import (
    page_account, page_privacy, page_terms, page_forgot_password,
)
from .domains.dashboard import (
    page_pharmacist_dashboard, page_admin_dashboard, page_admin_analytics,
    page_admin_claims, page_admin_moderation, page_admin_pharmacies,
    page_admin_shortages, page_admin_users,
)
