#!/bin/bash
# Самовосстановление прода — ставит две страховки «если сервис отвалится»:
#   1) systemd Restart=always для gunicorn/daphne (падение процесса → подъём за секунды)
#   2) watchdog-крон: раз в минуту проверяет /chat/ локально; 2 фейла подряд → рестарт
# Идемпотентно (можно запускать сколько угодно раз). НЕ трогает сетевые блипы Timeweb
# (там приложение живо, сеть сама восстанавливается за ~1 мин).
#
# Запуск (от root): bash /var/www/workspace-app/deploy/setup_resilience.sh
set -e

# 1. systemd авто-рестарт (drop-in, оригинальные юниты не трогаем)
for svc in gunicorn daphne; do
  mkdir -p "/etc/systemd/system/$svc.service.d"
  cat > "/etc/systemd/system/$svc.service.d/restart.conf" <<'CONF'
[Service]
Restart=always
RestartSec=3
CONF
done
systemctl daemon-reload

# 2. watchdog
cat > /root/healthcheck.sh <<'HC'
#!/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
LOG=/root/healthcheck.log
code=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 12 -H 'Host: consolidatorparts.com' https://127.0.0.1/chat/ 2>/dev/null)
if [ "$code" = "200" ]; then rm -f /tmp/health_fail; exit 0; fi
n=$(( $(cat /tmp/health_fail 2>/dev/null || echo 0) + 1 )); echo "$n" > /tmp/health_fail
echo "$(date -u '+%F %T')Z health=$code fail#$n" >> "$LOG"
if [ "$n" -ge 2 ]; then
  echo "$(date -u '+%F %T')Z -> RESTART gunicorn daphne" >> "$LOG"
  systemctl restart gunicorn daphne
  rm -f /tmp/health_fail
fi
HC
chmod +x /root/healthcheck.sh

# 3. cron каждую минуту (идемпотентно; PATH и auto_deploy не трогаем)
( crontab -l 2>/dev/null | grep -v healthcheck.sh; echo '* * * * * /bin/bash /root/healthcheck.sh' ) | crontab -

echo "=== Restart-политика ==="; systemctl show gunicorn daphne -p Id -p Restart | paste - -
echo "=== crontab ==="; crontab -l | grep -E 'healthcheck|auto_deploy' || true
echo "=== self-test watchdog (ждём 200) ==="; /bin/bash /root/healthcheck.sh; echo "health_fail=$(cat /tmp/health_fail 2>/dev/null || echo none)"
echo "RESILIENCE SETUP DONE"
