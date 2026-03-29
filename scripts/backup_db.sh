#!/bin/bash
# Daily database backup script
# Add to crontab: 0 3 * * * /var/www/workspace-app/scripts/backup_db.sh

BACKUP_DIR="/var/www/backups"
DB_NAME="workspace_db"
DB_USER="workspace_user"
DATE=$(date +%Y%m%d_%H%M%S)
KEEP_DAYS=14

mkdir -p "$BACKUP_DIR"

# PostgreSQL backup
PGPASSWORD="$DB_PASSWORD" pg_dump -U "$DB_USER" "$DB_NAME" | gzip > "$BACKUP_DIR/${DB_NAME}_${DATE}.sql.gz"

# Remove old backups
find "$BACKUP_DIR" -name "*.sql.gz" -mtime +$KEEP_DAYS -delete

echo "Backup completed: ${DB_NAME}_${DATE}.sql.gz"
