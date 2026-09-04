#!/bin/bash
# ==============================================================================
# 🚀 BUA Student ID Portal – One-Click Automated VPS Setup Script
# Target OS: Ubuntu 22.04 LTS / 24.04 LTS (Hostinger VPS)
# ==============================================================================

set -e

# Color definitions
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${BLUE}======================================================================${NC}"
echo -e "${BLUE}  🎓 BUA Student ID Portal – VPS Automated Provisioning Script       ${NC}"
echo -e "${BLUE}======================================================================${NC}"

# Check for root
if [ "$EUID" -ne 0 ]; then
  echo -e "${RED}[ERROR] Please run this script as root or with sudo:${NC}"
  echo "  sudo bash deploy/setup_vps.sh"
  exit 1
fi

APP_DIR="/var/www/bua"
LOG_DIR="/var/log/bua"
BACKUP_DIR="/var/backups/bua"
DB_NAME="bua_db"
DB_USER="bua_user"
# Generate secure random DB password
DB_PASS=$(openssl rand -hex 16)

echo -e "\n${YELLOW}[1/8] Updating Ubuntu system packages...${NC}"
apt-get update -y
apt-get upgrade -y

echo -e "\n${YELLOW}[2/8] Installing prerequisites (Python, PostgreSQL, Nginx, libpq)...${NC}"
apt-get install -y \
  python3 \
  python3-pip \
  python3-venv \
  python3-dev \
  postgresql \
  postgresql-contrib \
  nginx \
  libpq-dev \
  git \
  ufw \
  certbot \
  python3-certbot-nginx \
  libgl1 \
  libglib2.0-0

echo -e "\n${YELLOW}[3/8] Configuring Local PostgreSQL Database...${NC}"
systemctl start postgresql
systemctl enable postgresql

# Create database and user safely
sudo -u postgres psql -c "SELECT 1 FROM pg_database WHERE datname = '$DB_NAME'" | grep -q 1 || \
sudo -u postgres psql -c "CREATE DATABASE $DB_NAME;"

sudo -u postgres psql -c "SELECT 1 FROM pg_roles WHERE rolname = '$DB_USER'" | grep -q 1 || \
sudo -u postgres psql -c "CREATE USER $DB_USER WITH ENCRYPTED PASSWORD '$DB_PASS';"

sudo -u postgres psql -c "ALTER USER $DB_USER WITH ENCRYPTED PASSWORD '$DB_PASS';"
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE $DB_NAME TO $DB_USER;"
sudo -u postgres psql -c "ALTER DATABASE $DB_NAME OWNER TO $DB_USER;"
sudo -u postgres psql -d $DB_NAME -c "GRANT ALL ON SCHEMA public TO $DB_USER;"

echo -e "${GREEN}[OK] PostgreSQL configured (Database: $DB_NAME, User: $DB_USER)${NC}"

echo -e "\n${YELLOW}[4/8] Setting up Application Directory & Virtual Environment...${NC}"
mkdir -p "$APP_DIR"
mkdir -p "$LOG_DIR"
mkdir -p "$BACKUP_DIR"
mkdir -p "$APP_DIR/static/uploads"

# Setup Python Virtual Environment
if [ ! -d "$APP_DIR/venv" ]; then
    echo "  • Creating Python virtualenv in $APP_DIR/venv..."
    python3 -m venv "$APP_DIR/venv"
fi

echo "  • Installing Python dependencies from requirements.txt..."
"$APP_DIR/venv/bin/pip" install --upgrade pip setuptools wheel
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

echo -e "\n${YELLOW}[5/8] Configuring Production Environment (.env)...${NC}"
ENV_FILE="$APP_DIR/.env"
if [ ! -f "$ENV_FILE" ]; then
    echo "  • Creating new .env file from template..."
    cp "$APP_DIR/deploy/.env.production.example" "$ENV_FILE"
    sed -i "s/YOUR_DB_PASSWORD/$DB_PASS/g" "$ENV_FILE"
else
    echo "  • Updating existing .env file with local DB credentials..."
    sed -i "s|DATABASE_URL=.*|DATABASE_URL=postgresql://$DB_USER:$DB_PASS@localhost:5432/$DB_NAME|g" "$ENV_FILE"
    sed -i "s/USE_NEON_HTTP=.*/USE_NEON_HTTP=false/g" "$ENV_FILE"
fi

echo -e "\n${YELLOW}[6/8] Migrating Database Schema & Data...${NC}"
cd "$APP_DIR"
# Run init_db to create tables
"$APP_DIR/venv/bin/python" -c "from database import init_db; init_db()"

# Import data dump if present
if [ -f "$APP_DIR/deploy/bua_backup.sql" ]; then
    echo "  • Importing existing students, users, and audit records..."
    sudo -u postgres psql "$DB_NAME" < "$APP_DIR/deploy/bua_backup.sql" || true
    echo -e "${GREEN}[OK] Existing data imported successfully!${NC}"
fi

# Set proper permissions for www-data
chown -R www-data:www-data "$APP_DIR"
chown -R www-data:www-data "$LOG_DIR"
chmod -R 755 "$APP_DIR/static/uploads"

echo -e "\n${YELLOW}[7/8] Installing Systemd Service & Nginx Server...${NC}"
# 1. Systemd Service
cp "$APP_DIR/deploy/bua.service" /etc/systemd/system/bua.service
systemctl daemon-reload
systemctl enable bua.service
systemctl restart bua.service

# 2. Nginx Site
cp "$APP_DIR/deploy/nginx.conf" /etc/nginx/sites-available/bua
ln -sf /etc/nginx/sites-available/bua /etc/nginx/sites-enabled/bua
rm -f /etc/nginx/sites-enabled/default

# Test Nginx syntax
nginx -t
systemctl restart nginx

# 3. Configure Firewall (UFW)
echo "  • Configuring UFW firewall rules..."
ufw allow OpenSSH
ufw allow 'Nginx Full'
ufw --force enable

# 4. Schedule Automated Daily Backups at 3:00 AM
echo "  • Scheduling automated daily backup cron job..."
chmod +x "$APP_DIR/deploy/backup.sh"
(crontab -l 2>/dev/null | grep -v "deploy/backup.sh" ; echo "0 3 * * * $APP_DIR/deploy/backup.sh >> /var/log/bua/backup.log 2>&1") | crontab -

echo -e "\n${YELLOW}[8/8] Verifying System Services Status...${NC}"
sleep 3
if systemctl is-active --quiet bua; then
    echo -e "${GREEN}  ✔ Application Service (bua.service) is RUNNING!${NC}"
else
    echo -e "${RED}  ✖ bua.service failed to start. Check logs: journalctl -u bua -n 50${NC}"
fi

if systemctl is-active --quiet nginx; then
    echo -e "${GREEN}  ✔ Web Server (Nginx) is RUNNING!${NC}"
fi

if systemctl is-active --quiet postgresql; then
    echo -e "${GREEN}  ✔ Database Server (PostgreSQL) is RUNNING!${NC}"
fi

SERVER_IP=$(hostname -I | awk '{print $1}')

echo -e "\n${BLUE}======================================================================${NC}"
echo -e "${GREEN}  🎉 DEPLOYMENT COMPLETED SUCCESSFULLY!${NC}"
echo -e "${BLUE}======================================================================${NC}"
echo -e "  • Web Portal URL:    ${YELLOW}http://${SERVER_IP}${NC}"
echo -e "  • Superadmin Login:  admin@university.edu.eg"
echo -e "  • Database:          PostgreSQL Local (${DB_NAME})"
echo -e "  • Photos Storage:    Local NVMe (${APP_DIR}/static/uploads)"
echo -e "  • Daily Backups:     Automated at 3:00 AM -> ${BACKUP_DIR}"
echo -e "\n👉 Next step for SSL (HTTPS):"
echo -e "  If you have a domain, point it to ${SERVER_IP} then run:"
echo -e "  ${YELLOW}sudo certbot --nginx -d yourdomain.com${NC}\n"
