#!/usr/bin/env bash
# Ежедневный бэкап PostgreSQL для Consolidator Parts.
# Хранит последние $KEEP дампов, старые удаляет. Идемпотентен.
#
# Ручной запуск:   bash deploy/backup.sh
# Cron (ставится deploy.sh): 03:30 ежедневно → /var/log/consolidator-backup.log
set -euo pipefail

APP_DIR="/var/www/workspace-app"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/consolidator}"
KEEP="${KEEP:-7}"

cd "$APP_DIR"
# Значения из .env (DB_NAME / DATABASE_URL)
set -a; . ./.env 2>/dev/null || true; set +a

DBN="${DB_NAME:-}"
if [[ -z "$DBN" && -n "${DATABASE_URL:-}" ]]; then
    DBN=$(echo "$DATABASE_URL" | sed -E 's#.*/([^/?]+).*#\1#')
fi
[[ -n "$DBN" ]] || { echo "$(date '+%F %T') ✗ не удалось определить имя БД"; exit 1; }

mkdir -p "$BACKUP_DIR"
STAMP=$(date +%Y%m%d_%H%M%S)
OUT="$BACKUP_DIR/${DBN}_${STAMP}.dump"

# -Fc = custom формат (сжатый, для pg_restore). Дамп под ролью postgres.
if sudo -u postgres pg_dump -Fc "$DBN" > "$OUT" 2>/tmp/backup.err; then
    echo "$(date '+%F %T') ✓ бэкап: $OUT ($(du -h "$OUT" | cut -f1))"
else
    echo "$(date '+%F %T') ✗ pg_dump упал:"; cat /tmp/backup.err; rm -f "$OUT"; exit 1
fi

# Ротация: оставить последние $KEEP, остальные удалить.
ls -1t "$BACKUP_DIR"/*.dump 2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm -f
echo "$(date '+%F %T') хранится дампов: $(ls -1 "$BACKUP_DIR"/*.dump 2>/dev/null | wc -l) (лимит $KEEP)"
