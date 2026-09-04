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

# 1. Backup PostgreSQL Database
echo "  • Dumping PostgreSQL database 'bua_db'..."
sudo -u postgres pg_dump bua_db > "${TEMP_DIR}/database.sql"

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

# 4. Cleanup old backups (keep last 14 days)
echo "  • Cleaning up backups older than 14 days..."
find "$BACKUP_DIR" -name "bua_backup_*.tar.gz" -type f -mtime +14 -delete

echo "[OK] Backup routine completed successfully."
