#!/usr/bin/env bash
# Проверяет последнюю копию восстановлением в отдельную временную базу.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${APP_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/consolidator}"

cd "$APP_DIR"
DB_USER="${DB_USER:-consolidator}"
COMPOSE_ARGS=(-f "$APP_DIR/docker-compose.yml")
if [[ -f "$APP_DIR/docker-compose.prod.yml" ]]; then
    COMPOSE_ARGS+=(-f "$APP_DIR/docker-compose.prod.yml")
fi

compose() {
    docker compose "${COMPOSE_ARGS[@]}" "$@"
}

DUMP="${1:-}"
if [[ -z "$DUMP" ]]; then
    DUMP="$(ls -1t "$BACKUP_DIR"/*.dump 2>/dev/null | head -1 || true)"
fi
[[ -n "$DUMP" && -f "$DUMP" ]] || {
    echo "Резервная копия для проверки не найдена" >&2
    exit 1
}

TEMP_DB="consolidator_restore_check_$(date +%s)_$$"
cleanup() {
    compose exec -T db dropdb -U "$DB_USER" --if-exists --force "$TEMP_DB" >/dev/null 2>&1 || true
}
trap cleanup EXIT

compose exec -T db pg_restore --list <"$DUMP" >/dev/null
compose exec -T db createdb -U "$DB_USER" "$TEMP_DB"
compose exec -T db pg_restore \
    -U "$DB_USER" \
    --exit-on-error \
    --no-owner \
    --no-privileges \
    --dbname "$TEMP_DB" <"$DUMP"

TABLE_COUNT="$(
    compose exec -T db psql -U "$DB_USER" -d "$TEMP_DB" -Atqc \
        "SELECT count(*) FROM pg_catalog.pg_tables WHERE schemaname='public'"
)"
[[ "${TABLE_COUNT:-0}" -gt 0 ]] || {
    echo "В восстановленной базе нет таблиц" >&2
    exit 1
}

MIGRATION_OK="$(
    compose exec -T db psql -U "$DB_USER" -d "$TEMP_DB" -Atqc \
        "SELECT 1 FROM django_migrations LIMIT 1"
)"
[[ "$MIGRATION_OK" == "1" ]] || {
    echo "В восстановленной базе не найдена история миграций" >&2
    exit 1
}

echo "Проверка восстановления успешна: $(basename "$DUMP"), таблиц: $TABLE_COUNT"
