"""
Management command: create or update the platform admin/superuser.

Usage:
  python manage.py create_admin

Reads from env vars:
  DJANGO_ADMIN_USER     (default: admin)
  DJANGO_ADMIN_EMAIL    (default: admin@consolidatorparts.com)
  DJANGO_ADMIN_PASSWORD (required if user doesn't exist)

Idempotent: safe to run on every deploy.
"""
from __future__ import annotations

import os

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Create or ensure the platform superuser account."

    def handle(self, *args, **options):
        username = os.getenv("DJANGO_ADMIN_USER", "admin").strip()
        email    = os.getenv("DJANGO_ADMIN_EMAIL", "admin@consolidatorparts.com").strip()
        password = os.getenv("DJANGO_ADMIN_PASSWORD", "").strip()

        existing_user = User.objects.filter(username=username).first()
        if existing_user is None and not password:
            raise CommandError(
                "DJANGO_ADMIN_PASSWORD is required when creating a new administrator."
            )
        if (
            existing_user is not None
            and not existing_user.is_superuser
            and not password
        ):
            raise CommandError(
                "DJANGO_ADMIN_PASSWORD is required when promoting an existing user."
            )

        user, created = User.objects.get_or_create(
            username=username,
            defaults={"email": email, "is_staff": True, "is_superuser": True},
        )

        if created:
            user.set_password(password)
            user.email = email
            user.is_staff = True
            user.is_superuser = True
            user.save()
            self.stdout.write(self.style.SUCCESS(
                f"Admin user '{username}' created with provided password."
            ))
        else:
            changed = False
            if password:
                user.set_password(password)
                changed = True
            if not user.is_superuser:
                user.is_superuser = True
                user.is_staff = True
                changed = True
            if user.email != email and email:
                user.email = email
                changed = True
            if changed:
                user.save()
                self.stdout.write(self.style.SUCCESS(
                    f"Admin user '{username}' updated."
                ))
            else:
                self.stdout.write(f"Admin user '{username}' already exists — no changes.")
