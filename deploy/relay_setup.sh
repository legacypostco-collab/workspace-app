#!/bin/bash
# Тонкий релей Anthropic для прода в РФ.
# Запускать НА не-РФ VPS (Amsterdam/Frankfurt), под root.
# Принцип: TCP/TLS passthrough — прод соединяется TLS напрямую с Anthropic
# СКВОЗЬ релей; ключ на релее не виден (end-to-end TLS прод↔Anthropic).
set -e
PROD_IP="${PROD_IP:?Перед запуском задайте PROD_IP рабочей системы}"

echo "=== 1. nginx + stream-модуль ==="
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y nginx libnginx-mod-stream ufw curl

echo "=== 2. stream-конфиг (passthrough :443 → api.anthropic.com:443) ==="
cat > /etc/nginx/stream-anthropic.conf <<'CONF'
stream {
    resolver 1.1.1.1 8.8.8.8 valid=60s ipv6=off;
    server {
        listen 443;
        set $anthropic "api.anthropic.com:443";
        proxy_pass $anthropic;
        proxy_connect_timeout 10s;
        proxy_timeout 300s;
    }
}
CONF
# include в main-контекст (идемпотентно)
grep -q 'stream-anthropic.conf' /etc/nginx/nginx.conf || \
  printf '\ninclude /etc/nginx/stream-anthropic.conf;\n' >> /etc/nginx/nginx.conf

echo "=== 3. фаервол: SSH всем (только по ключу), 443 ТОЛЬКО с прода ==="
ufw allow 22/tcp
ufw allow from ${PROD_IP} to any port 443 proto tcp
ufw --force enable
ufw status numbered

echo "=== 4. проверка + рестарт ==="
nginx -t
systemctl enable nginx
systemctl restart nginx

echo "=== 5. self-test: достаёт ли САМ релей до Anthropic (ждём НЕ 403) ==="
curl -s -o /dev/null -w "  relay -> anthropic HTTP=%{http_code}\n" --max-time 12 \
  https://api.anthropic.com/v1/messages || true
echo "=== ГОТОВО. IP этого сервера -> прописать на проде в /etc/hosts. ==="
