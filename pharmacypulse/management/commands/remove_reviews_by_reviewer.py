"""Delete reviews whose displayed reviewer names are explicitly listed.

Add exact reviewer names to REVIEWER_NAMES, then preview with:
  python manage.py remove_reviews_by_reviewer

Apply the deletion with:
  python manage.py remove_reviews_by_reviewer --apply

Matching is case-insensitive and ignores leading/trailing whitespace. Reviewer
accounts and pharmacy records are not deleted.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models.functions import Lower, Trim

from pharmacypulse.models import Review


# Enter exact reviewer names as they appear on their reviews.
# Keep this empty until you are ready to configure the names yourself.
REVIEWER_NAMES: list[str] = ["test user", "new test"]


class Command(BaseCommand):
    help = "Preview or delete reviews by their displayed reviewer names"

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Delete matching reviews; without this flag, only preview them",
        )
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Skip the confirmation prompt (requires --apply)",
        )

    def handle(self, *args, **options):
        if options["yes"] and not options["apply"]:
            raise CommandError("--yes can only be used together with --apply.")

        names = self._get_names()
        if not names:
            raise CommandError(
                "No reviewer names configured. Add exact names to REVIEWER_NAMES "
                "in remove_reviews_by_reviewer.py."
            )

        reviews = (
            Review.objects.annotate(normalized_user_name=Lower(Trim("user_name")))
            .filter(normalized_user_name__in=names)
            .select_related("user", "pharmacy")
            .order_by("user_name", "id")
        )
        matches = list(reviews)

        self.stdout.write(self.style.WARNING(
            f"Found {len(matches)} review(s) matching {len(names)} reviewer name(s):"
        ))
        for review in matches:
            account_email = review.user.email if review.user_id else "no linked account"
            pharmacy = review.pharmacy.name if review.pharmacy_id else (
                review.pharmacy_name or "unknown pharmacy"
            )
            status = "soft-deleted" if review.deleted_at else review.moderation_status
            self.stdout.write(
                f"  Review #{review.id}: reviewer={review.user_name!r}, "
                f"pharmacy={pharmacy!r}, account={account_email!r}, status={status}"
            )

        if not matches:
            self.stdout.write("No matching reviews; nothing to do.")
            return

        if not options["apply"]:
            self.stdout.write(self.style.NOTICE(
                "\nPreview only. Re-run with --apply to delete these reviews."
            ))
            return

        if not options["yes"]:
            confirmation = input(
                f"Type DELETE to permanently remove {len(matches)} review(s): "
            )
            if confirmation != "DELETE":
                self.stdout.write("Cancelled; no changes were made.")
                return

        review_ids = [review.id for review in matches]
        with transaction.atomic():
            deleted_count, deleted_by_model = Review.objects.filter(
                id__in=review_ids
            ).delete()

        response_count = deleted_by_model.get("pharmacypulse.ReviewResponse", 0)
        self.stdout.write(self.style.SUCCESS(
            f"Deleted {deleted_count} record(s), including {len(review_ids)} review(s) "
            f"and {response_count} review response(s). Reviewer accounts and "
            "pharmacy records were kept."
        ))

    @staticmethod
    def _get_names() -> list[str]:
        names = []
        seen = set()
        for raw_name in REVIEWER_NAMES:
            name = raw_name.strip()
            normalized = name.casefold()
            if not name:
                raise CommandError("REVIEWER_NAMES must not contain blank names.")
            if normalized in seen:
                raise CommandError(f"Duplicate reviewer name in REVIEWER_NAMES: {name!r}")
            seen.add(normalized)
            names.append(name)
        return names
