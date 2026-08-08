#!/usr/bin/env bash
# Ежедневная резервная копия PostgreSQL для Docker-развертывания.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${APP_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/consolidator}"
KEEP="${KEEP:-14}"

cd "$APP_DIR"
DB_NAME="${DB_NAME:-consolidator}"
DB_USER="${DB_USER:-consolidator}"
COMPOSE_ARGS=(-f "$APP_DIR/docker-compose.yml")
if [[ -f "$APP_DIR/docker-compose.prod.yml" ]]; then
    COMPOSE_ARGS+=(-f "$APP_DIR/docker-compose.prod.yml")
fi

compose() {
    docker compose "${COMPOSE_ARGS[@]}" "$@"
}

[[ "$KEEP" =~ ^[1-9][0-9]*$ ]] || {
    echo "$(date '+%F %T') неверное значение KEEP: $KEEP" >&2
    exit 1
}

umask 0077
install -d -m 0700 "$BACKUP_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$BACKUP_DIR/${DB_NAME}_${STAMP}.dump"
TMP="$OUT.tmp"
ERROR_LOG="$BACKUP_DIR/.backup-error.log"
cleanup() { rm -f "$TMP"; }
trap cleanup EXIT

if ! compose exec -T db pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc >"$TMP" 2>"$ERROR_LOG"; then
    echo "$(date '+%F %T') резервное копирование PostgreSQL завершилось ошибкой" >&2
    cat "$ERROR_LOG" >&2
    exit 1
fi

if ! compose exec -T db pg_restore --list <"$TMP" >/dev/null 2>"$ERROR_LOG"; then
    echo "$(date '+%F %T') созданный дамп не прошел проверку формата" >&2
    cat "$ERROR_LOG" >&2
    exit 1
fi

mv "$TMP" "$OUT"
rm -f "$ERROR_LOG"

index=0
while IFS= read -r dump; do
    index=$((index + 1))
    if ((index > KEEP)); then
        rm -f -- "$dump"
    fi
done < <(ls -1t "$BACKUP_DIR"/*.dump 2>/dev/null || true)

stored="$(find "$BACKUP_DIR" -maxdepth 1 -type f -name '*.dump' | wc -l | tr -d ' ')"
echo "$(date '+%F %T') резервная копия готова: $OUT ($(du -h "$OUT" | cut -f1)); хранится $stored, лимит $KEEP"
