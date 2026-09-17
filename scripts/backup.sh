#!/bin/bash
set -e

BACKUP_DIR="/var/backups/bua"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
TEMP_DIR="/tmp/bua_backup_${TIMESTAMP}"
ARCHIVE_NAME="${BACKUP_DIR}/bua_backup_${TIMESTAMP}.tar.gz"

mkdir -p "$BACKUP_DIR"
mkdir -p "$TEMP_DIR"

echo "[*] [${TIMESTAMP}] Starting BUA Portal Backup..."

# 1. Backup PostgreSQL Database
echo "  • Dumping PostgreSQL database 'bua_db'..."
if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^bua_postgres$"; then
    docker exec -t bua_postgres pg_dump -U bua_user bua_db > "${TEMP_DIR}/database.sql"
elif command -v pg_dump &>/dev/null; then
    sudo -u postgres pg_dump bua_db > "${TEMP_DIR}/database.sql"
fi

# 2. Backup Local Uploads / MinIO persistent volume
echo "  • Copying student photos..."
if [ -d "/var/www/bua/static/uploads" ]; then
    cp -r /var/www/bua/static/uploads "${TEMP_DIR}/uploads" 2>/dev/null || true
fi

# 3. Create Compressed Archive
echo "  • Creating compressed tar.gz archive..."
tar -czf "$ARCHIVE_NAME" -C "$TEMP_DIR" .
rm -rf "$TEMP_DIR"

echo "[OK] Backup created successfully: ${ARCHIVE_NAME}"
echo "     Size: $(du -sh "$ARCHIVE_NAME" | cut -f1)"

# 4. Optional Upload Backup to Google Drive
if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^bua_app$"; then
    echo "  • Syncing backup archive to Google Drive via bua_app container..."
    docker cp "$ARCHIVE_NAME" bua_app:/tmp/bua_backup.tar.gz
    docker exec -i bua_app python -c "
import os, sys
try:
    from gdrive_helper import upload_backup_to_gdrive, is_gdrive_configured
    if is_gdrive_configured():
        ok = upload_backup_to_gdrive('/tmp/bua_backup.tar.gz', 'bua_backup.tar.gz')
        if ok:
            print('[OK] Backup uploaded to Google Drive successfully!')
        else:
            print('[WARNING] Google Drive upload did not complete.')
    else:
        print('[INFO] Google Drive sync skipped (not configured or sync disabled).')
except Exception as e:
    print(f'[WARNING] Google Drive backup sync error: {e}')
" || true
    docker exec bua_app rm -f /tmp/bua_backup.tar.gz || true
fi

# 5. Cleanup old backups (keep last 14 days)
echo "  • Cleaning up backups older than 14 days..."
find "$BACKUP_DIR" -name "bua_backup_*.tar.gz" -type f -mtime +14 -delete

echo "[OK] Backup routine completed successfully."
