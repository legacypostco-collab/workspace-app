#!/usr/bin/env bash
# Запустить E2E-тесты.
#   1. Поднимает (или переиспользует) dev сервер на :8003
#   2. Прогоняет pytest через все tests/e2e/*.py
#   3. Возвращает exit code pytest'а
#
# Опции (env):
#   E2E_HEADED=1          — показывать браузер (default headless)
#   E2E_SLOW_MO=200       — задержка между действиями в ms (для отладки)
#   E2E_BASE_URL=http://… — URL сервера (default http://127.0.0.1:8003)
#   SKIP_SERVER=1         — не запускать сервер, ожидать что уже работает

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$APP_DIR"

PORT="${E2E_PORT:-8003}"
URL="${E2E_BASE_URL:-http://127.0.0.1:${PORT}}"

# 1. Поднять сервер если нужен
SERVER_PID=""
cleanup() {
    if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
        echo "Stopping dev server (PID $SERVER_PID)…"
        kill "$SERVER_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT

if [ "${SKIP_SERVER:-0}" != "1" ]; then
    if curl -s -o /dev/null "$URL/login/"; then
        echo "Reusing existing server at $URL"
    else
        echo "Starting dev server at $URL…"
        DB_ENGINE=django.db.backends.sqlite3 \
        DB_NAME=db.sqlite3 \
        SECRET_KEY=devsecret \
        ALLOWED_HOSTS='*' \
        DEBUG_MODE=True \
        CHANNELS_INMEMORY=true \
        CELERY_TASK_ALWAYS_EAGER=true \
        CELERY_BROKER_URL=memory:// \
        ./venv/bin/python manage.py runserver "127.0.0.1:${PORT}" --noreload > /tmp/e2e_server.log 2>&1 &
        SERVER_PID=$!
        # Ждём пока поднимется
        for i in {1..15}; do
            if curl -s -o /dev/null "$URL/login/"; then
                echo "  ↑ ready"
                break
            fi
            sleep 1
        done
    fi
fi

# 2. pytest
echo "Running E2E tests against $URL…"
exec ./venv/bin/pytest tests/e2e/ -v --tb=short "$@"
