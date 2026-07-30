#!/usr/bin/env bash
# Production deploy для Consolidator Parts на /var/www/workspace-app.
#
# Использование:
#   ssh deploy@<PRODUCTION_HOST> 'cd /var/www/workspace-app && bash deploy/deploy.sh'
#
# Опции:
#   bash deploy/deploy.sh              # обычный деплой (git pull + migrate + restart)
#   bash deploy/deploy.sh --rollback   # откат на предыдущий commit
#   bash deploy/deploy.sh --dry-run    # показать migrate --plan без применения
#
# Идемпотентен. Каждый шаг безопасен к повторному запуску.

set -euo pipefail

APP_DIR="/var/www/workspace-app"
VENV="$APP_DIR/venv"
BRANCH="${DEPLOY_BRANCH:-main}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1/}"

cd "$APP_DIR"
log() { echo "[$(date '+%F %T')] $*"; }
die() { log "✗ $*"; exit 1; }

# ── Rollback ─────────────────────────────────────────────
if [[ "${1:-}" == "--rollback" ]]; then
    PREV=$(git rev-parse HEAD@{1} 2>/dev/null) || die "no previous HEAD"
    log "Rolling back to $PREV"
    git reset --hard "$PREV"
    "$VENV/bin/python" manage.py collectstatic --noinput --clear > /dev/null
    systemctl restart gunicorn daphne
    systemctl reload nginx
    log "✓ Rolled back to $PREV"
    exit 0
fi

DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

# ── Pre-flight: validate .env ────────────────────────────
log "━━━ 0. pre-flight checks ━━━"
[[ -f .env ]] || die ".env missing"
# Загружаем ЗНАЧЕНИЯ из .env (а не только имена) — иначе проверки ниже всегда
# видят пустые переменные и падают на «SECRET_KEY required».
set -a; . ./.env; set +a
require_secret() {
    local name="$1" min_length="$2" value="${!1:-}"
    [[ "${#value}" -ge "$min_length" ]] || die ".env: $name must contain at least $min_length characters"
    case "$value" in
        *CHANGE_ME*|dev-secret*|dev-only*|django-insecure-*|whsec_...|sk_live_...)
            die ".env: $name still contains a known placeholder"
            ;;
    esac
}
require_secret SECRET_KEY 50
require_secret QR_SECRET 32
require_secret PAYMENT_CALLBACK_SECRET 32
[[ -n "${DATABASE_URL:-}" ]]            || die ".env: DATABASE_URL required"
case "${DEBUG_MODE:-}" in
    1|true|True|TRUE|yes|Yes|YES|on|On|ON)
        die ".env: DEBUG_MODE must be False in production"
        ;;
esac
[[ -n "${ALLOWED_HOSTS:-}" ]]           || die ".env: ALLOWED_HOSTS required"
[[ -n "${CSRF_TRUSTED_ORIGINS:-}" ]]    || die ".env: CSRF_TRUSTED_ORIGINS required"
[[ -n "${ANTHROPIC_API_KEY:-}" ]] || log "  ⚠ ANTHROPIC_API_KEY missing — AI will use stub heuristics"
[[ "${PAYMENT_ENGINE:-wallet}" == "wallet" ]] || die ".env: only PAYMENT_ENGINE=wallet is production-ready"
if [[ -n "${TELEGRAM_BOT_TOKEN:-}" ]]; then
    require_secret TELEGRAM_WEBHOOK_SECRET 32
fi
log "  ✓ env validated"

# ── 0b. OOM guard: swap (idempotent) ─────────────────────
# На боксе 4GB RAM без swap daphne+postgres под нагрузкой ловят OOM-killer →
# сервис «постоянно падает». 4G swap + низкий swappiness снимают краш-цикл.
# Блок безопасен к повторному запуску и переживает ребут (fstab).
log "━━━ 0b. OOM guard (swap) ━━━"
if ! swapon --show=NAME --noheadings 2>/dev/null | grep -q '/swapfile'; then
    if [[ ! -f /swapfile ]]; then
        fallocate -l 4G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=4096 status=none
        chmod 600 /swapfile
        mkswap /swapfile >/dev/null 2>&1
    fi
    swapon /swapfile 2>/dev/null && log "  + swap включён (4G)" || log "  ⚠ swapon не удался"
else
    log "  ✓ swap уже активен"
fi
if ! grep -q '/swapfile' /etc/fstab 2>/dev/null; then
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
    log "  + swap прописан в /etc/fstab (переживёт ребут)"
fi
if [[ "$(cat /proc/sys/vm/swappiness 2>/dev/null || echo 60)" != "10" ]]; then
    sysctl -w vm.swappiness=10 >/dev/null 2>&1 || true
    grep -q '^vm.swappiness' /etc/sysctl.conf 2>/dev/null || echo 'vm.swappiness=10' >> /etc/sysctl.conf
    log "  + swappiness=10 (мягче вытесняет в swap)"
fi

log "━━━ 1. git pull origin $BRANCH ━━━"
BEFORE=$(git rev-parse HEAD)
git fetch origin
git checkout "$BRANCH"
git reset --hard "origin/$BRANCH"
AFTER=$(git rev-parse HEAD)
if [[ "$BEFORE" == "$AFTER" ]]; then
    log "  (no changes — $AFTER)"
else
    log "  $BEFORE → $AFTER"
    git --no-pager log --oneline "$BEFORE..$AFTER" | head -10
fi

log "━━━ 2. pip install ━━━"
if [[ "$BEFORE" != "$AFTER" ]] && git diff --name-only "$BEFORE..$AFTER" | grep -q '^requirements.txt$'; then
    "$VENV/bin/pip" install --upgrade pip --quiet
    "$VENV/bin/pip" install -r requirements.txt --quiet
    log "  ✓ deps updated"
else
    log "  (no changes)"
fi
"$VENV/bin/python" manage.py check_deploy_readiness

log "━━━ 3. migrate --plan ━━━"
"$VENV/bin/python" manage.py migrate --plan 2>&1 | head -30
if [[ "$DRY_RUN" == "1" ]]; then
    log "→ dry-run mode, exiting before apply"
    exit 0
fi
log "━━━ 3b. migrate --noinput ━━━"
"$VENV/bin/python" manage.py migrate --noinput

log "━━━ 4. collectstatic ━━━"
"$VENV/bin/python" manage.py collectstatic --noinput --clear > /tmp/collect.log 2>&1 || {
    tail -20 /tmp/collect.log
    die "collectstatic failed"
}
log "  $(tail -1 /tmp/collect.log)"

# daphne runs as www-data; ensure all upload targets are writable by it.
mkdir -p "$APP_DIR"/media/{pricelists,drawings,kyb,claims_evidence,parts,brands,categories,onboarding,catalog,part_images,claims,imports}
chown -R www-data:www-data "$APP_DIR"/media
chmod -R u+rwX,g+rX "$APP_DIR"/media
log "  ✓ media perms (www-data)"

log "━━━ 5. compilemessages ━━━"
"$VENV/bin/python" manage.py compilemessages 2>&1 | tail -3 || true

log "━━━ 6. systemd units (idempotent) ━━━"
if [[ ! -f /etc/systemd/system/daphne.socket ]]; then
    cp deploy/daphne.socket /etc/systemd/system/daphne.socket
    cp deploy/daphne.service /etc/systemd/system/daphne.service
    systemctl daemon-reload
    systemctl enable --now daphne.socket
    log "  + installed daphne units"
else
    if ! cmp -s deploy/daphne.service /etc/systemd/system/daphne.service; then
        cp deploy/daphne.service /etc/systemd/system/daphne.service
        systemctl daemon-reload
        log "  ~ refreshed daphne.service"
    fi
fi

log "━━━ 7. nginx config ━━━"
# Используем новый nginx.conf (HTTPS + CSP + WS + gzip + brotli)
NGINX_TARGET=/etc/nginx/sites-available/consolidator
if [[ -f deploy/nginx.conf ]] && ! cmp -s deploy/nginx.conf "$NGINX_TARGET" 2>/dev/null; then
    cp deploy/nginx.conf "$NGINX_TARGET"
    ln -sf "$NGINX_TARGET" /etc/nginx/sites-enabled/consolidator
    [[ -e /etc/nginx/sites-enabled/default ]] && rm -f /etc/nginx/sites-enabled/default
    nginx -t > /dev/null 2>&1 || die "nginx config invalid"
    log "  ~ nginx config refreshed"
fi

log "━━━ 8. restart services ━━━"
systemctl restart gunicorn 2>/dev/null || true
systemctl restart daphne
sleep 3
systemctl is-active --quiet daphne || {
    journalctl -u daphne -n 30 --no-pager
    die "daphne failed to start"
}
systemctl reload nginx

# Celery worker/beat — рестартим ВСЕГДА (если юнит есть). Иначе воркер держит
# СТАРЫЙ код в sys.modules: деплой обновит файлы, но фоновые задачи (импорт
# прайса) будут исполнять прежнюю версию — тихий и опасный баг. Прежний
# `grep -q '^celery\.service' && restart` под set -e мог молча пропустить рестарт.
for _svc in celery celery-beat; do
    if systemctl cat "$_svc" >/dev/null 2>&1; then
        systemctl restart "$_svc" 2>/dev/null && log "  ✓ restarted $_svc" || log "  ⚠ $_svc restart failed"
    fi
done

log "━━━ 9. health checks ━━━"
sleep 2
for path in "/" "/robots.txt" "/sitemap.xml"; do
    code=$(curl -s -o /dev/null -w '%{http_code}' "${HEALTH_URL%/}$path" 2>/dev/null || echo "000")
    case "$code" in
        200|301|302) log "  ✓ $path → $code" ;;
        *) die "✗ $path → $code (run: bash deploy/deploy.sh --rollback)" ;;
    esac
done

# Security headers smoke
csp=$(curl -sI "$HEALTH_URL" | grep -ic '^content-security-policy:' || true)
[[ "$csp" -ge 1 ]] && log "  ✓ CSP present" || log "  ⚠ CSP missing"

log "━━━ 10. daily DB backup cron ━━━"
# Ежедневный бэкап БД (03:30) с ротацией — обязательная страховка для prod.
chmod +x deploy/backup.sh 2>/dev/null || true
if [[ ! -f /etc/cron.d/consolidator-backup ]]; then
    cat > /etc/cron.d/consolidator-backup <<'CRON'
# Consolidator: ежедневный бэкап БД в 03:30, хранит 7 последних дампов
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
30 3 * * * root /var/www/workspace-app/deploy/backup.sh >> /var/log/consolidator-backup.log 2>&1
CRON
    chmod 0644 /etc/cron.d/consolidator-backup
    log "  + установлен daily backup cron (03:30 · хранит 7)"
else
    log "  ✓ backup cron уже установлен"
fi

log "━━━ 11. healthcheck watchdog (авто-рестарт daphne при зависании) ━━━"
# daphne может «зависнуть» (процесс жив, но не отвечает на HTTP) — systemd
# Restart=always тут НЕ помогает (нет exit-кода). Cron каждую минуту проверяет
# HTTP и рестартит daphne при зависании → макс ~1 мин простоя вместо «до ручного
# ребута». Это лечит первопричину повторных обвалов прод-сервиса.
cat > /usr/local/bin/consolidator-watchdog.sh <<'WD'
#!/usr/bin/env bash
chk() { curl -sk --max-time 8 -H 'Host: consolidatorparts.com' -o /dev/null -w '%{http_code}' https://127.0.0.1/ 2>/dev/null || echo 000; }
c=$(chk)
case "$c" in 200|301|302) exit 0 ;; esac
sleep 5
c=$(chk)
case "$c" in 200|301|302) exit 0 ;; esac
echo "$(date '+%F %T') watchdog: HTTP=$c → restart daphne" >> /var/log/consolidator-watchdog.log
systemctl restart daphne
sleep 3
systemctl reload nginx 2>/dev/null || true
WD
chmod +x /usr/local/bin/consolidator-watchdog.sh
if [[ ! -f /etc/cron.d/consolidator-watchdog ]]; then
    cat > /etc/cron.d/consolidator-watchdog <<'WDC'
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
* * * * * root /usr/local/bin/consolidator-watchdog.sh
WDC
    chmod 0644 /etc/cron.d/consolidator-watchdog
    log "  + watchdog cron установлен (проверка каждую минуту)"
else
    log "  ✓ watchdog уже установлен"
fi

log "━━━ STATUS ━━━"
systemctl --no-pager status daphne nginx 2>&1 | grep -E "(●|Active:|Main PID)" | head -10

log "✓ Deploy complete — $AFTER live"
