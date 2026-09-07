#!/usr/bin/env bash
# ==============================================================================
# BUA Student ID Portal - Docker Deployment Script for VPS
# ==============================================================================
set -e

echo "🚀 Starting BUA Portal Docker Deployment..."

# 1. Install Docker & Docker Compose if not installed
if ! command -v docker &> /dev/null; then
    echo "📦 Docker not found. Installing Docker Engine..."
    sudo apt-get update
    sudo apt-get install -y ca-certificates curl gnupg lsb-release
    sudo mkdir -p /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
    sudo apt-get update
    sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    sudo systemctl enable --now docker
    echo "✅ Docker installed successfully."
fi

# 2. Check for .env file
cd "$(dirname "$0")/.."
if [ ! -f .env ]; then
    if [ -f deploy/.env.production.example ]; then
        echo "⚠️ .env file not found. Creating from deploy/.env.production.example..."
        cp deploy/.env.production.example .env
        echo "❗ Please review and update your .env file with production credentials before continuing."
    else
        echo "❌ .env file missing! Please create .env first."
        exit 1
    fi
fi

# 3. Pull/Build and start containers with docker compose
echo "🔨 Building and starting Docker services (PostgreSQL + Web App)..."
docker compose down --remove-orphans
docker compose up -d --build

# 4. Wait for database health check
echo "⏳ Waiting for PostgreSQL container to become healthy..."
TIMEOUT=30
ELAPSED=0
while [ $ELAPSED -lt $TIMEOUT ]; do
    STATUS=$(docker inspect --format='{{json .State.Health.Status}}' bua_postgres 2>/dev/null || echo "starting")
    if [ "$STATUS" = '"healthy"' ]; then
        echo "✅ PostgreSQL is healthy and accepting connections!"
        break
    fi
    sleep 2
    ELAPSED=$((ELAPSED + 2))
    echo "   ...waiting ($ELAPSED/$TIMEOUT s)"
done

# 5. Display container status
echo ""
echo "=================================================="
echo "🎉 Deployment Completed Successfully!"
echo "=================================================="
docker compose ps
echo ""
echo "App is live on: http://$(curl -s ifconfig.me 2>/dev/null || echo 'YOUR_SERVER_IP'):5000"
