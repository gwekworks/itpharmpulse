"""Strip the legacy [purpose] [timeframe] prefixes that were incorrectly
prepended to review comment text before being saved to the DB.

Examples of what gets cleaned:
  "[refill] [today] Great pharmacy"        → "Great pharmacy"
  "[refill,new_rx] [yesterday] test"       → "test"
  "[refill] [today]"                        → ""  (comment becomes empty/null)
  "My experience was good at this pharmacy" → unchanged (no prefix)

Run with:
  python manage.py clean_review_comments
  python manage.py clean_review_comments --dry-run   (preview only, no DB writes)
"""
from __future__ import annotations

import re

from django.core.management.base import BaseCommand

from pharmacypulse.models import Review


# Matches the bracketed prefix pattern at the START of the comment:
# one or two bracket groups, each containing alphanumeric values / commas / underscores,
# followed by optional whitespace.
# e.g. "[refill]", "[refill,new_rx]", "[yesterday]", "[today]", "[this_week]"
_PREFIX_RE = re.compile(
    r"^\s*"                       # optional leading whitespace
    r"(?:\[[a-zA-Z0-9_,\s]+\]\s*){1,3}"  # 1-3 bracketed groups
, re.IGNORECASE)


def strip_prefix(comment: str) -> str:
    """Remove the bracketed prefix from a comment string."""
    if not comment:
        return comment
    cleaned = _PREFIX_RE.sub("", comment).strip()
    return cleaned


class Command(BaseCommand):
    help = "Strip [purpose] [timeframe] prefixes from legacy review comments"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview changes without writing to the database",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — no changes will be saved\n"))

        # Only look at reviews whose comment starts with a "[" — fast index scan
        candidates = Review.objects.filter(
            comment__startswith="[",
            deleted_at__isnull=True,
        ).only("id", "comment")

        updated = 0
        skipped = 0

        for review in candidates:
            original = review.comment or ""
            cleaned = strip_prefix(original)

            if cleaned == original:
                skipped += 1
                continue

            self.stdout.write(
                f"  Review #{review.id}:\n"
                f"    Before: {original!r}\n"
                f"    After:  {cleaned!r}\n"
            )

            if not dry_run:
                Review.objects.filter(id=review.id).update(
                    comment=cleaned if cleaned else None
                )
            updated += 1

        action = "Would update" if dry_run else "Updated"
        self.stdout.write(
            self.style.SUCCESS(
                f"\n{action} {updated} review(s). {skipped} had brackets but no match."
            )
        )
