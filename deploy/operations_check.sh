#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/var/www/workspace-app}"
VENV="${VENV:-$APP_DIR/venv}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/consolidator}"
MAX_BACKUP_AGE_HOURS="${MAX_BACKUP_AGE_HOURS:-30}"
cd "$APP_DIR"
set -a; . ./.env 2>/dev/null || true; set +a

for service in daphne nginx celery celery-beat; do
    if systemctl cat "$service" >/dev/null 2>&1; then
        systemctl is-active --quiet "$service" || { echo "$service is not active"; exit 1; }
    fi
done

"$VENV/bin/python" manage.py check_operations --require-worker

LATEST=$(find "$BACKUP_DIR" -maxdepth 1 -type f -name '*.dump' -print0 \
    | xargs -0 -r ls -1t 2>/dev/null | head -1 || true)
[[ -n "$LATEST" ]] || { echo "No database backup found"; exit 1; }
NOW=$(date +%s)
MODIFIED=$(stat -c %Y "$LATEST")
AGE_HOURS=$(( (NOW - MODIFIED) / 3600 ))
[[ "$AGE_HOURS" -le "$MAX_BACKUP_AGE_HOURS" ]] || {
    echo "Latest database backup is ${AGE_HOURS}h old"; exit 1;
}

if [[ -n "${UPTIME_HEARTBEAT_URL:-}" ]]; then
    curl -fsS --max-time 8 "$UPTIME_HEARTBEAT_URL" >/dev/null
fi
echo "Operations check OK: backup_age=${AGE_HOURS}h"
