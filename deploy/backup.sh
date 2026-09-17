#!/bin/bash
# ====================================================================
# BUA Student ID Portal – Automated Backup Script
# Backs up PostgreSQL database + All Student Photos on NVMe
# ====================================================================

set -e

BACKUP_DIR="/var/backups/bua"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
TEMP_DIR="/tmp/bua_backup_${TIMESTAMP}"
ARCHIVE_NAME="${BACKUP_DIR}/bua_backup_${TIMESTAMP}.tar.gz"

mkdir -p "$BACKUP_DIR"
mkdir -p "$TEMP_DIR"

echo "[*] [${TIMESTAMP}] Starting BUA Portal Backup..."

# 1. Backup PostgreSQL Database (Supports Docker container or local system)
echo "  • Dumping PostgreSQL database 'bua_db'..."
if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^bua_postgres$"; then
    echo "    (Using Docker container bua_postgres)"
    docker exec -t bua_postgres pg_dump -U bua_user bua_db > "${TEMP_DIR}/database.sql"
elif command -v pg_dump &>/dev/null; then
    echo "    (Using host pg_dump)"
    sudo -u postgres pg_dump bua_db > "${TEMP_DIR}/database.sql"
else
    echo "    [WARNING] Could not find pg_dump or running bua_postgres container."
fi

# 2. Copy Student Photos
echo "  • Copying student photos from /var/www/bua/static/uploads/..."
if [ -d "/var/www/bua/static/uploads" ]; then
    cp -r /var/www/bua/static/uploads "${TEMP_DIR}/uploads"
fi

# 3. Create Compressed Archive
echo "  • Creating compressed tar.gz archive..."
tar -czf "$ARCHIVE_NAME" -C "$TEMP_DIR" .
rm -rf "$TEMP_DIR"

echo "[OK] Backup created successfully: ${ARCHIVE_NAME}"
echo "     Size: $(du -sh "$ARCHIVE_NAME" | cut -f1)"

# 4. Upload Backup to Google Drive (if configured)
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
elif [ -f "/var/www/bua/venv/bin/python" ]; then
    echo "  • Syncing backup archive to Google Drive..."
    /var/www/bua/venv/bin/python -c "
import os, sys
sys.path.insert(0, '/var/www/bua')
try:
    from gdrive_helper import upload_backup_to_gdrive, is_gdrive_configured
    if is_gdrive_configured():
        ok = upload_backup_to_gdrive('$ARCHIVE_NAME', 'bua_backup.tar.gz')
        if ok:
            print('[OK] Backup uploaded to Google Drive successfully!')
        else:
            print('[WARNING] Google Drive upload did not complete.')
    else:
        print('[INFO] Google Drive sync skipped (not configured in .env).')
except Exception as e:
    print(f'[WARNING] Google Drive backup sync error: {e}')
" || true
fi

# 5. Cleanup old backups (keep last 14 days)
echo "  • Cleaning up backups older than 14 days..."
find "$BACKUP_DIR" -name "bua_backup_*.tar.gz" -type f -mtime +14 -delete

echo "[OK] Backup routine completed successfully."
