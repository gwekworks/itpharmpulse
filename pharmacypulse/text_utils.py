"""Tiny shared text utilities. Lives outside management/commands so it can
be imported from page_data, models, etc. without circular-import worries."""
from __future__ import annotations

import re
from django.utils.text import slugify

# Trailing store-number suffix patterns. Real-world chain names look like:
#   'CVS PHARMACY # 08910'        ('#NNNN' pattern with optional spaces)
#   'WALGREENS #09001'
#   'OSCO DRUG #3224'
#   'WALMART PHARMACY 10-0049'    (division-store, two chunks: ' 10' + '-0049')
#   'SAMS PHARMACY 10-4703'
_TRAILING_NUM_CHUNK = re.compile(r"(?:\s*[#\-]\s*\d{1,6}|\s+\d{1,6})\s*$")


def strip_chain_suffix(name: str) -> str:
    """Return `name` with trailing store-number suffix(es) removed. Preserves
    case so display callers get the original casing back. Loop-strips so
    multi-chunk suffixes like ' 10-0049' get fully removed.

    Used for display only — the DB stores the full NPPES name including
    suffix so we don't lose information. Chain-detection grouping also uses
    this function (after .upper())."""
    if not name:
        return ""
    s = name.strip()
    while True:
        m = _TRAILING_NUM_CHUNK.search(s)
        if not m or m.start() <= 2:
            break
        s = s[:m.start()].rstrip()
    return s


def pharmacy_slug(name: str) -> str:
    """Generate an SEO-friendly URL slug for a pharmacy's detail page.

    Uses the chain-suffix-stripped display name so generic chain pages get a
    clean keyword slug. Slugs are not unique by design — the numeric pharmacy
    id in the URL is the canonical locator; the slug is purely descriptive
    (e.g. `/pharmacy/1234/cvs-pharmacy/`).
    """
    base = strip_chain_suffix(name) or name or "pharmacy"
    slug = slugify(base)
    # Collapse overly-long slugs; keep them short enough to look tidy in
    # search results while still carrying the chain/locale keywords.
    return slug[:80].rstrip("-") or "pharmacy"
