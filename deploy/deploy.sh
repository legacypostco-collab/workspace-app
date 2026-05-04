#!/usr/bin/env bash
# Production deploy script for workspace-app on /var/www/workspace-app.
#
# Запуск (на проде, под root):
#   cd /var/www/workspace-app && bash deploy/deploy.sh
#
# Что делает:
#   1. Тянет свежий main
#   2. Обновляет venv (новые deps: daphne, channels, channels-redis, anthropic, ...)
#   3. Применяет миграции (новая 0007 + всё что было после)
#   4. Собирает статику
#   5. Устанавливает/обновляет daphne.service + daphne.socket (если ещё нет)
#   6. Обновляет nginx config (если ещё нет)
#   7. Перезапускает gunicorn + daphne, reload nginx
#
# Идемпотентен — можно запускать сколько угодно раз.

set -euo pipefail

APP_DIR="/var/www/workspace-app"
VENV="$APP_DIR/venv"
BRANCH="${DEPLOY_BRANCH:-main}"

cd "$APP_DIR"

echo "━━━ 1. git pull origin $BRANCH ━━━"
git fetch origin
git checkout "$BRANCH"
git reset --hard "origin/$BRANCH"

echo "━━━ 2. pip install ━━━"
"$VENV/bin/pip" install --upgrade pip --quiet
"$VENV/bin/pip" install -r requirements.txt --quiet

echo "━━━ 3. migrate ━━━"
"$VENV/bin/python" manage.py migrate --noinput

echo "━━━ 4. collectstatic ━━━"
"$VENV/bin/python" manage.py collectstatic --noinput --clear

echo "━━━ 5. systemd units (idempotent) ━━━"
# Daphne для WebSocket
if [ ! -f /etc/systemd/system/daphne.socket ]; then
    cp deploy/daphne.socket /etc/systemd/system/daphne.socket
    cp deploy/daphne.service /etc/systemd/system/daphne.service
    systemctl daemon-reload
    systemctl enable --now daphne.socket
    echo "  + installed daphne.service / daphne.socket"
else
    # Refresh from repo if changed
    if ! cmp -s deploy/daphne.service /etc/systemd/system/daphne.service; then
        cp deploy/daphne.service /etc/systemd/system/daphne.service
        systemctl daemon-reload
        echo "  ~ refreshed daphne.service"
    fi
fi

echo "━━━ 6. nginx config ━━━"
NGINX_TARGET=/etc/nginx/sites-available/workspace-app
if [ ! -f "$NGINX_TARGET" ] || ! cmp -s deploy/nginx-prod.conf "$NGINX_TARGET"; then
    cp deploy/nginx-prod.conf "$NGINX_TARGET"
    ln -sf "$NGINX_TARGET" /etc/nginx/sites-enabled/workspace-app
    # Удалить дефолтный сайт, если он мешает
    [ -e /etc/nginx/sites-enabled/default ] && rm -f /etc/nginx/sites-enabled/default
    nginx -t
    echo "  ~ refreshed nginx config"
fi

echo "━━━ 7. restart services ━━━"
systemctl restart gunicorn
systemctl restart daphne
systemctl reload nginx

# Поднять celery, если есть
if systemctl list-unit-files | grep -q '^celery\.service'; then
    systemctl restart celery
fi

echo
echo "━━━ STATUS ━━━"
systemctl --no-pager status gunicorn daphne nginx 2>&1 | grep -E "(●|Active:|Main PID)" | head -20

echo
echo "━━━ HEALTH ━━━"
curl -s -o /dev/null -w "HTTP /  → %{http_code} (%{time_total}s)\n" http://127.0.0.1/ || true
curl -s -o /dev/null -w "HTTP /chat/ → %{http_code} (%{time_total}s)\n" http://127.0.0.1/chat/ -H "Host: 72.56.234.89" || true

echo
echo "✓ deploy complete. App at http://72.56.234.89/"
