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


@register.simple_tag(takes_context=True)
def url_replace(context, **kwargs):
    """Return current request query string with specified parameters updated/added/removed.
    Passing a parameter as '' or None removes it from the query string.
    """
    request = context.get("request")
    if not request:
        return ""
    query = request.GET.copy()
    for k, v in kwargs.items():
        if v is None or v == "":
            query.pop(k, None)
        else:
            query[k] = str(v)
    encoded = query.urlencode()
    return f"?{encoded}" if encoded else "?"


_PURPOSE_LABELS = {
    'refill':       ('🔄', 'Refill'),
    'new_rx':       ('📋', 'New Rx'),
    'transfer':     ('🔁', 'Transfer'),
    'vaccine':      ('💉', 'Vaccine'),
    'consultation': ('💬', 'Consultation'),
    'other':        ('•••', 'Other'),
}

_TIMEFRAME_LABELS = {
    'today':      'Today',
    'yesterday':  'Yesterday',
    'this_week':  'This week',
    'last_week':  'Last week',
    'this_month': 'This month',
}


@register.filter
def visit_purpose_badges(value):
    """Return a list of {'icon': ..., 'label': ...} dicts from a comma-separated
    visit_purpose string, e.g. 'refill,new_rx' → [{'icon':'🔄','label':'Refill'}, ...]"""
    if not value:
        return []
    badges = []
    for p in str(value).split(','):
        p = p.strip()
        if p in _PURPOSE_LABELS:
            icon, label = _PURPOSE_LABELS[p]
            badges.append({'icon': icon, 'label': label})
        elif p:
            badges.append({'icon': '', 'label': p.replace('_', ' ').title()})
    return badges


@register.filter
def visit_timeframe_label(value):
    """Return a human-readable label for a visit_timeframe value."""
    if not value:
        return ''
    return _TIMEFRAME_LABELS.get(str(value).strip(), str(value).replace('_', ' ').title())
