#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${1:-${APP_DIR:-/srv/consolidator-itunity}}"
PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3}"
STATE_DIR="${MONITOR_STATE_DIR:-/var/lib/consolidator-monitor}"

[[ "$EUID" -eq 0 ]] || { echo "Run as root" >&2; exit 1; }
[[ -f "$APP_DIR/deploy/monitoring_controller.py" ]] || {
    echo "Monitoring controller not found in $APP_DIR" >&2
    exit 1
}

install -d -m 0750 "$STATE_DIR"
chmod 0755 "$APP_DIR/deploy/monitoring_controller.py"

cat > /etc/systemd/system/consolidator-monitor.service <<UNIT
[Unit]
Description=Consolidator operational monitoring controller
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$APP_DIR
Environment=APP_DIR=$APP_DIR
EnvironmentFile=-$APP_DIR/.env
ExecStart=$PYTHON_BIN $APP_DIR/deploy/monitoring_controller.py --app-dir $APP_DIR
TimeoutStartSec=180
UMask=0027
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=$STATE_DIR

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/consolidator-monitor.timer <<'UNIT'
[Unit]
Description=Run Consolidator operational checks every minute

[Timer]
OnBootSec=90s
OnUnitActiveSec=60s
AccuracySec=10s
Persistent=true
RandomizedDelaySec=5s

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now consolidator-monitor.timer
systemctl start consolidator-monitor.service || true
systemctl --no-pager --full status consolidator-monitor.timer
