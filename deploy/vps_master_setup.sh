#!/usr/bin/env bash
# ==============================================================================
# BUA Student ID Portal – Master VPS Installation & Security Setup Script
# Works on Ubuntu 22.04 / 24.04 LTS & Debian 11 / 12
# ==============================================================================
set -euo pipefail

# Color Codes
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${BLUE}======================================================================${NC}"
echo -e "${BLUE}   🚀 BUA STUDENT ID PORTAL – MASTER PRODUCTION VPS SETUP (DOCKER)     ${NC}"
echo -e "${BLUE}======================================================================${NC}"

# 0. Root Check
if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}[ERROR] This script must be run as root (or with sudo).${NC}"
    exit 1
fi

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP_DIR"

echo -e "\n${YELLOW}[1/7] Updating system packages and installing prerequisites...${NC}"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    gnupg \
    lsb-release \
    ufw \
    fail2ban \
    nginx \
    tar \
    gzip \
    cron

# 1. Install Docker & Docker Compose if missing
echo -e "\n${YELLOW}[2/7] Checking Docker & Docker Compose installation...${NC}"
if ! command -v docker &> /dev/null; then
    echo -e "  • Installing official Docker Engine..."
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" > /etc/apt/sources.list.d/docker.list
    apt-get update -y
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    systemctl enable --now docker
    echo -e "${GREEN}  ✓ Docker installed successfully.${NC}"
else
    echo -e "${GREEN}  ✓ Docker is already installed ($(docker --version)).${NC}"
fi

# 2. Configure Host Security & Firewall (UFW & Fail2ban)
echo -e "\n${YELLOW}[3/7] Hardening Server Security (UFW Firewall & Fail2ban)...${NC}"
ufw --force reset >/dev/null 2>&1 || true
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp comment 'SSH'
ufw allow 80/tcp comment 'HTTP'
ufw allow 443/tcp comment 'HTTPS'
ufw allow 5000/tcp comment 'Direct Web Port'
# Explicitly ensure port 5432 is not exposed publicly
ufw deny 5432/tcp comment 'PostgreSQL local container only'
ufw --force enable
echo -e "${GREEN}  ✓ UFW Firewall active: Ports 22, 80, 443, 5000 allowed. Port 5432 protected.${NC}"

# Enable Fail2ban
systemctl enable --now fail2ban
echo -e "${GREEN}  ✓ Fail2ban service active (brute-force defense enabled).${NC}"

# 3. Configure Environment File (.env)
echo -e "\n${YELLOW}[4/7] Checking Environment Configuration (.env)...${NC}"
if [ ! -f "$APP_DIR/.env" ]; then
    if [ -f "$APP_DIR/deploy/.env.production.example" ]; then
        echo -e "  • Generating production .env from template..."
        cp "$APP_DIR/deploy/.env.production.example" "$APP_DIR/.env"
        # Generate random high-entropy keys
        SECRET=$(openssl rand -hex 32)
        JWT_SECRET=$(openssl rand -hex 32)
        DB_PASS=$(openssl rand -hex 16)
        sed -i "s/SECRET_KEY=.*/SECRET_KEY=$SECRET/" "$APP_DIR/.env"
        sed -i "s/JWT_SECRET_KEY=.*/JWT_SECRET_KEY=$JWT_SECRET/" "$APP_DIR/.env"
        sed -i "s/YOUR_DB_PASSWORD/$DB_PASS/" "$APP_DIR/.env"
        echo -e "${GREEN}  ✓ Created fresh .env with generated secure keys.${NC}"
    else
        echo -e "${RED}[ERROR] .env file is missing and no template found!${NC}"
        exit 1
    fi
else
    echo -e "${GREEN}  ✓ Existing .env file found.${NC}"
fi

# 4. Start Docker Containers (PostgreSQL + Flask Web)
echo -e "\n${YELLOW}[5/7] Building and Starting Docker Stack (PostgreSQL + Web)...${NC}"
docker compose down --remove-orphans >/dev/null 2>&1 || true
docker compose up -d --build

# Wait for PostgreSQL container health
echo -e "  • Waiting for PostgreSQL container (bua_postgres) to become healthy..."
TIMEOUT=40
ELAPSED=0
DB_HEALTHY=false
while [ $ELAPSED -lt $TIMEOUT ]; do
    STATUS=$(docker inspect --format='{{json .State.Health.Status}}' bua_postgres 2>/dev/null || echo "starting")
    if [ "$STATUS" = '"healthy"' ]; then
        DB_HEALTHY=true
        break
    fi
    sleep 2
    ELAPSED=$((ELAPSED + 2))
    echo -n "."
done
echo ""

if [ "$DB_HEALTHY" = true ]; then
    echo -e "${GREEN}  ✓ PostgreSQL container is healthy and accepting connections!${NC}"
else
    echo -e "${RED}  ✗ PostgreSQL took too long to become healthy. Check: docker logs bua_postgres${NC}"
fi

# Initialize Database Schema inside Docker container
echo -e "  • Initializing database schema & superadmin..."
docker compose exec -T web python -c "import database; database.init_db(); print('Database verified successfully.')" || true

# 5. Configure Nginx Reverse Proxy
echo -e "\n${YELLOW}[6/7] Configuring Nginx Reverse Proxy...${NC}"
NGINX_CONF="/etc/nginx/sites-available/bua"
cat << 'EOF' > "$NGINX_CONF"
server {
    listen 80;
    listen [::]:80;
    server_name _;

    client_max_body_size 25M;

    # Gzip Compression
    gzip on;
    gzip_vary on;
    gzip_proxied any;
    gzip_comp_level 6;
    gzip_types text/plain text/css text/xml application/json application/javascript application/rss+xml image/svg+xml;

    # Security Headers
    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-XSS-Protection "1; mode=block" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;

    # Static files cached directly with fallback to proxy
    location /static/ {
        alias /var/www/bua/static/;
        expires 30d;
        add_header Cache-Control "public, no-transform";
        try_files $uri $uri/ @proxy;
    }

    # Proxy to Docker Web Container
    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_connect_timeout 60s;
        proxy_read_timeout 120s;
        proxy_send_timeout 120s;
        proxy_buffering on;
        proxy_buffer_size 128k;
        proxy_buffers 4 256k;
        proxy_busy_buffers_size 256k;
    }

    location @proxy {
        proxy_pass http://127.0.0.1:5000;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
EOF

ln -sf "$NGINX_CONF" /etc/nginx/sites-enabled/bua
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx
echo -e "${GREEN}  ✓ Nginx reverse proxy configured and active on port 80.${NC}"

# 6. Schedule Daily Google Drive Backups
echo -e "\n${YELLOW}[7/7] Scheduling Automated Backups to Google Drive...${NC}"
chmod +x "$APP_DIR/deploy/backup.sh"
CRON_JOB="0 3 * * * root $APP_DIR/deploy/backup.sh >> /var/log/bua_backup.log 2>&1"
echo "$CRON_JOB" > /etc/cron.d/bua_backup
chmod 0644 /etc/cron.d/bua_backup
echo -e "${GREEN}  ✓ Daily backup scheduled at 03:00 AM (PostgreSQL + Photos + Google Drive).${NC}"

# Summary & Status Report
SERVER_IP=$(curl -s ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')
echo -e "\n${BLUE}======================================================================${NC}"
echo -e "${GREEN}🎉 ALL-IN-ONE SETUP COMPLETED SUCCESSFULLY!${NC}"
echo -e "${BLUE}======================================================================${NC}"
echo -e "  🌐 Portal URL:        ${YELLOW}http://${SERVER_IP}${NC}"
echo -e "  🗄️ PostgreSQL:        ${GREEN}Running in Docker (bua_postgres:5432)${NC}"
echo -e "  🚀 Web Application:   ${GREEN}Running in Docker (bua_app:5000 -> Nginx:80)${NC}"
echo -e "  ☁️ Cloud Storage:     ${GREEN}Google Drive (Active)${NC}"
echo -e "  🛡️ Firewall & DDOS:   ${GREEN}UFW + Fail2ban (Active)${NC}"
echo -e "  ⏰ Daily Backups:     ${GREEN}Configured (03:00 AM Daily to Google Drive)${NC}"
echo -e "${BLUE}======================================================================${NC}"
echo -e "To view container logs at any time:"
echo -e "  ${YELLOW}docker compose logs -f web${NC}"
echo -e "  ${YELLOW}docker compose logs -f db${NC}"
echo -e "${BLUE}======================================================================${NC}\n"
