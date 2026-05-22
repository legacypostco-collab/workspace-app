"""Shared pytest fixtures + Django setup for tests/."""
import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "consolidator_site.settings")
django.setup()
