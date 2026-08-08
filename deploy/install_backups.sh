#!/usr/bin/env bash
# Устанавливает резервное копирование и проверку восстановления через systemd.
set -euo pipefail

APP_DIR="${1:-${APP_DIR:-/srv/consolidator-itunity}}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/consolidator}"
KEEP="${KEEP:-14}"

[[ "$EUID" -eq 0 ]] || { echo "Запустите скрипт от root" >&2; exit 1; }
[[ -x "$APP_DIR/deploy/backup.sh" ]] || chmod 0755 "$APP_DIR/deploy/backup.sh"
[[ -x "$APP_DIR/deploy/verify_restore.sh" ]] || chmod 0755 "$APP_DIR/deploy/verify_restore.sh"
install -d -m 0700 "$BACKUP_DIR"

cat >/etc/systemd/system/consolidator-backup.service <<UNIT
[Unit]
Description=Consolidator database backup
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$APP_DIR
Environment=APP_DIR=$APP_DIR
Environment=BACKUP_DIR=$BACKUP_DIR
Environment=KEEP=$KEEP
ExecStart=/usr/bin/env bash $APP_DIR/deploy/backup.sh
TimeoutStartSec=1800
UMask=0077

[Install]
WantedBy=multi-user.target
UNIT

cat >/etc/systemd/system/consolidator-backup.timer <<'UNIT'
[Unit]
Description=Run Consolidator database backup daily

[Timer]
OnCalendar=*-*-* 03:30:00 UTC
Persistent=true
RandomizedDelaySec=10m

[Install]
WantedBy=timers.target
UNIT

cat >/etc/systemd/system/consolidator-restore-check.service <<UNIT
[Unit]
Description=Verify latest Consolidator database backup
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$APP_DIR
Environment=APP_DIR=$APP_DIR
Environment=BACKUP_DIR=$BACKUP_DIR
ExecStart=/usr/bin/env bash $APP_DIR/deploy/verify_restore.sh
TimeoutStartSec=1800
UMask=0077

[Install]
WantedBy=multi-user.target
UNIT

cat >/etc/systemd/system/consolidator-restore-check.timer <<'UNIT'
[Unit]
Description=Verify Consolidator database backup weekly

[Timer]
OnCalendar=Sun *-*-* 04:15:00 UTC
Persistent=true
RandomizedDelaySec=15m

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now consolidator-backup.timer consolidator-restore-check.timer
systemctl start consolidator-backup.service
systemctl start consolidator-restore-check.service
systemctl --no-pager --full status consolidator-backup.timer consolidator-restore-check.timer
