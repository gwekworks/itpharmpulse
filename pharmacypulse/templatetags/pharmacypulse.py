"""Custom template filters for PharmacyPulse.

Only `initials` survives the native-Django cleanup — every other ported Benmore
filter has a stock equivalent: `truncate` → `truncatechars`, `as_date` → `date`,
`ago`/`timeago` → `timesince`. `currency`, `pct`, `stars`, `has_scope` were
unused after the cleanup and removed.
"""
from __future__ import annotations

from django import template

register = template.Library()


@register.filter
def initials(value):
    """Return uppercase initials from a name (e.g. 'Richard Buehling' → 'RB')."""
    if not value:
        return ""
    parts = str(value).strip().split()
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0][:1].upper()
    return (parts[0][:1] + parts[-1][:1]).upper()


@register.filter
def intcomma_safe(value):
    """Format integers with thousands separators; '' for None / non-numeric."""
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return ""


@register.filter
def get_item(value, key):
    """Dictionary access by key (e.g. `hours_map|get_item:dow`). Returns None
    when the dict (or key) is missing so templates render empty, not errors."""
    if value is None:
        return None
    try:
        return value.get(key)
    except (AttributeError, TypeError):
        return None
