#!/usr/bin/env bash
#
# OWASP ZAP Baseline Scan — пассивный аудит безопасности.
#
# Запускает:
#   - HTTP-spider всех публичных URL'ов
#   - Passive scan (без attack payload'ов — safe для prod)
#   - Сохраняет HTML/JSON report
#
# Что детектит ZAP baseline:
#   - Missing security headers (CSP, HSTS, X-Frame-Options, X-Content-Type-Options)
#   - Cookies без Secure/HttpOnly/SameSite
#   - Disclosed sensitive info (server version, stack traces)
#   - Outdated JS libraries with known CVEs
#   - Reflected URL params в response (XSS hints)
#
# НЕ делает active attacks (нужен `zap-full-scan.py` для SQLi/XSS payload'ов
# — тот может реально создать данные в БД, делать ТОЛЬКО на staging).
#
# Usage:
#   bash tests/security/zap_baseline.sh https://beta.your-domain.tld
#   bash tests/security/zap_baseline.sh                  # default http://127.0.0.1:8001
#
# Требует docker (для официального zap-stable образа):
#   docker pull ghcr.io/zaproxy/zaproxy:stable

set -euo pipefail

TARGET="${1:-http://host.docker.internal:8001}"
REPORT_DIR="${REPORT_DIR:-tests/security/zap-reports}"
mkdir -p "$REPORT_DIR"

TS=$(date +%Y%m%d-%H%M%S)
HTML="$REPORT_DIR/zap-baseline-$TS.html"
JSON="$REPORT_DIR/zap-baseline-$TS.json"

echo "→ Target:       $TARGET"
echo "→ HTML report:  $HTML"
echo "→ JSON report:  $JSON"
echo ""

docker run --rm \
  -v "$(pwd)/$REPORT_DIR:/zap/wrk:rw" \
  -t ghcr.io/zaproxy/zaproxy:stable \
  zap-baseline.py \
    -t "$TARGET" \
    -r "$(basename "$HTML")" \
    -J "$(basename "$JSON")" \
    -m 5 \
    -d \
  || EXIT=$?  # ZAP returns non-zero on warnings — это нормально

echo ""
echo "✓ Done. Open $HTML in browser to review."
echo ""
echo "Common ZAP findings to triage:"
echo "  • CSP — проверяем отсутствие разрешения встроенных обработчиков и стилей"
echo "  • HSTS — настраивается через env SECURE_HSTS_SECONDS"
echo "  • Cookie SameSite — Django ставит Lax by default"
echo "  • X-XSS-Protection — устарел, не нужно"
exit ${EXIT:-0}
