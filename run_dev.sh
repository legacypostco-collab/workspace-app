#!/bin/bash
# Dev server accessible via Tailscale (100.94.17.47:8001)
# Uses SQLite (no PostgreSQL required)

cd "$(dirname "$0")"

DB_ENGINE=django.db.backends.sqlite3 \
DB_NAME='' \
SECURE_SSL_REDIRECT=False \
SESSION_COOKIE_SECURE=False \
CSRF_COOKIE_SECURE=False \
DEBUG_MODE=True \
SERVE_MEDIA=True \
ALLOWED_HOSTS="*" \
.venv/bin/python manage.py runserver 0.0.0.0:8001 --noreload
