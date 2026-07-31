#!/usr/bin/env bash
# Restores the latest custom-format dump into an isolated temporary database.
set -euo pipefail

APP_DIR="${APP_DIR:-/var/www/workspace-app}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/consolidator}"
cd "$APP_DIR"
set -a; . ./.env 2>/dev/null || true; set +a

DUMP="${1:-}"
if [[ -z "$DUMP" ]]; then
    DUMP=$(find "$BACKUP_DIR" -maxdepth 1 -type f -name '*.dump' -print0 \
        | xargs -0 -r ls -1t 2>/dev/null | head -1 || true)
fi
[[ -n "$DUMP" && -f "$DUMP" ]] || { echo "No database dump found"; exit 1; }

TEMP_DB="consolidator_restore_check_$(date +%s)_$$"
cleanup() { sudo -u postgres dropdb --if-exists "$TEMP_DB" >/dev/null 2>&1 || true; }
trap cleanup EXIT

pg_restore --list "$DUMP" >/dev/null
sudo -u postgres createdb "$TEMP_DB"
sudo -u postgres pg_restore \
    --exit-on-error --no-owner --no-privileges --dbname "$TEMP_DB" "$DUMP"

TABLE_COUNT=$(sudo -u postgres psql -Atqc \
    "SELECT count(*) FROM pg_catalog.pg_tables WHERE schemaname='public'" "$TEMP_DB")
[[ "${TABLE_COUNT:-0}" -gt 0 ]] || { echo "Restored database contains no tables"; exit 1; }
sudo -u postgres psql -Atqc \
    "SELECT 1 FROM django_migrations LIMIT 1" "$TEMP_DB" | grep -q '^1$'

echo "Restore check OK: $(basename "$DUMP"), tables=$TABLE_COUNT"
