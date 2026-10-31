"""Delete explicitly listed test accounts and their test-generated data.

Add exact email addresses to EMAILS_TO_DELETE, then preview with:
  python manage.py remove_test_accounts

Apply the cleanup with:
  python manage.py remove_test_accounts --apply

The command never deletes pharmacy records. It clears claim/organization
associations, removes authored reviews and close reports, and deletes accounts.
"""
from __future__ import annotations

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Q
from django.db.models.functions import Lower

from pharmacypulse.models import (
    ClosedFlag,
    Pharmacy,
    PharmacyClaim,
    PharmacyOrg,
    PharmacyPublishable,
    PharmacyTeamMember,
    Review,
    User,
)


# Enter the exact account email addresses to remove. The list is intentionally
# empty so running the command before editing it cannot affect any accounts.
EMAILS_TO_DELETE: list[str] = [
    "digitallyahead1@gmail.com",
    "samuel.o@codelabprojects.com",
    "jeffery@codelabprojects.com",
    "mania.ollie@gmail.com",
    "praise@codelabprojects.com",
    "test@codelabprojects.com",
    "test@gmail.com",
    "judith@codelabprojects.com",
    "precious.edward@codelabprojects.com",
    "titilope@codelabprojects.com",
    "victoria.ifeyinwa@codelabprojects.com",
    "precious.chioma@codelabprojects.com",
    "anna@odelabprojects.com",
    "deskmateassistant@gmail.com",
    "user@test.com",
    "pharmacist@test.com",
    "emmanuel@codelabprojects.com",
    "juliet.chukwuka@odelabprojects.com",
    "grace@codelabprojects.com",
    "testtest@test.com",
    "precious.chioma@gmail.com",
    "testniish@test.com",
    "ochidiebere071@gmail.com",
    "chidiebere@codelabprojects.com",
    "onowises@gmail.com",
    "test2@test.com",
    "test@test.com",
    ]


class Command(BaseCommand):
    help = "Preview or remove explicitly listed test accounts and related data"

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Perform the cleanup; without this flag, only preview the changes",
        )
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Skip the confirmation prompt (requires --apply)",
        )

    def handle(self, *args, **options):
        if options["yes"] and not options["apply"]:
            raise CommandError("--yes can only be used together with --apply.")

        emails = self._get_emails()
        if not emails:
            raise CommandError(
                "No email addresses configured. Add exact addresses to "
                "EMAILS_TO_DELETE in remove_test_accounts.py."
            )

        users = list(
            User.objects.annotate(normalized_email=Lower("email"))
            .filter(normalized_email__in=emails)
            .order_by("email")
        )
        found_emails = {user.email.strip().lower() for user in users}
        missing_emails = [email for email in emails if email not in found_emails]
        user_ids = [user.id for user in users]

        org_ids = list(
            PharmacyOrg.objects.filter(owner_id__in=user_ids).values_list("id", flat=True)
        )
        other_org_memberships = PharmacyTeamMember.objects.filter(
            org_id__in=org_ids,
        ).filter(Q(user_id__isnull=True) | ~Q(user_id__in=user_ids))

        self.stdout.write(self.style.WARNING("Account cleanup preview"))
        for email in missing_emails:
            self.stdout.write(f"  {email}: no matching account")

        for user in users:
            owned_org_ids = list(
                PharmacyOrg.objects.filter(owner_id=user.id).values_list("id", flat=True)
            )
            member_count = PharmacyTeamMember.objects.filter(
                org_id__in=owned_org_ids
            ).filter(Q(user_id__isnull=True) | ~Q(user_id__in=user_ids)).count()
            self.stdout.write(
                f"  {user.email} (user #{user.id}, role={user.role}): "
                f"{Review.objects.filter(user_id=user.id).count()} review(s), "
                f"{ClosedFlag.objects.filter(reporter_user_id=user.id).count()} "
                f"close report(s), "
                f"{PharmacyClaim.objects.filter(user_id=user.id).count()} "
                f"claim record(s), "
                f"{Pharmacy.objects.filter(claimed_by=user.id).count()} "
                f"pharmacy ownership link(s), "
                f"{len(owned_org_ids)} owned organization(s), "
                f"{member_count} other organization member(s) affected"
            )

        if not users:
            self.stdout.write("No matching accounts; nothing to do.")
            return

        self.stdout.write(
            "Pharmacy and publishable pharmacy records will be kept; "
            "their claim and organization links will be cleared as needed."
        )

        if other_org_memberships.exists():
            self.stdout.write(
                self.style.ERROR(
                    "Cleanup blocked: at least one owned organization has a "
                    "team membership for a user not in the deletion list. "
                    "Review or reassign those memberships before retrying."
                )
            )
            if options["apply"]:
                raise CommandError("No changes were made.")
            return

        admin_emails = [user.email for user in users if user.is_superuser or user.role == "admin"]
        if admin_emails:
            self.stdout.write(
                self.style.ERROR(
                    "Cleanup blocked for administrator account(s): "
                    + ", ".join(admin_emails)
                )
            )
            if options["apply"]:
                raise CommandError("No changes were made.")
            return

        if not options["apply"]:
            self.stdout.write(
                self.style.NOTICE(
                    "\nPreview only. Re-run with --apply to perform this cleanup."
                )
            )
            return

        if not options["yes"]:
            confirmation = input(
                f"Type DELETE to remove {len(users)} account(s) and their listed data: "
            )
            if confirmation != "DELETE":
                self.stdout.write("Cancelled; no changes were made.")
                return

        with transaction.atomic():
            target_ids = [user.id for user in users]

            reviews_deleted, _ = Review.objects.filter(user_id__in=target_ids).delete()
            reports_deleted, _ = ClosedFlag.objects.filter(
                reporter_user_id__in=target_ids
            ).delete()
            claims_deleted, _ = PharmacyClaim.objects.filter(
                user_id__in=target_ids
            ).delete()

            Pharmacy.objects.filter(claimed_by__in=target_ids).update(
                claimed_by=None,
                org_id=None,
            )
            PharmacyPublishable.objects.filter(claimed_by__in=target_ids).update(
                claimed_by=None,
                org_id=None,
            )

            # Deleting an owned organization would otherwise remove its
            # association from pharmacies through the FK's SET_NULL behavior.
            Pharmacy.objects.filter(org_id__in=org_ids).update(org_id=None)
            PharmacyPublishable.objects.filter(org_id__in=org_ids).update(org_id=None)

            users_deleted, _ = User.objects.filter(id__in=target_ids).delete()

        self.stdout.write(
            self.style.SUCCESS(
                f"Cleanup complete: {users_deleted} user-related record(s) deleted "
                f"(including accounts); {reviews_deleted} review-related record(s), "
                f"{reports_deleted} close-report record(s), and "
                f"{claims_deleted} claim record(s) deleted. "
                "Pharmacy records were retained."
            )
        )

    @staticmethod
    def _get_emails() -> list[str]:
        emails = []
        seen = set()
        for raw_email in EMAILS_TO_DELETE:
            email = raw_email.strip().lower()
            if not email:
                continue
            try:
                validate_email(email)
            except ValidationError as error:
                raise CommandError(f"Invalid email in EMAILS_TO_DELETE: {raw_email!r}") from error
            if email in seen:
                raise CommandError(f"Duplicate email in EMAILS_TO_DELETE: {email}")
            seen.add(email)
            emails.append(email)
        return emails
