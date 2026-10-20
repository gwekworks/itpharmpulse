"""Create three dummy users — one per role — for local testing.

Usage: python manage.py seed_dummy_users
Idempotent: re-running updates existing users (password reset to the default)
instead of failing on the unique-email constraint.
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

User = get_user_model()

DEFAULTS = [
    {"email": "admin@test.com",     "username": "dummy_admin",    "role": "admin",     "password": "AdminPass123!"},
    {"email": "pharmacist@test.com", "username": "dummy_pharmacist", "role": "pharmacist", "password": "PharmPass123!"},
    {"email": "user@test.com",      "username": "dummy_user",     "role": "user",      "password": "UserPass123!"},
]


class Command(BaseCommand):
    help = "Seed three dummy users: admin, pharmacist, and a regular user."

    def handle(self, *args, **options):
        for spec in DEFAULTS:
            user, created = User.objects.get_or_create(email=spec["email"], defaults={
                "username": spec["username"],
                "role": spec["role"],
            })
            user.username = spec["username"]
            user.role = spec["role"]
            user.is_active = True
            user.is_staff = spec["role"] == "admin"
            user.is_superuser = spec["role"] == "admin"
            user.set_password(spec["password"])
            user.save()

            state = "created" if created else "updated"
            self.stdout.write(self.style.SUCCESS(
                f"[{state}] {spec['role']:<10} {spec['email']} / {spec['password']}"
            ))
