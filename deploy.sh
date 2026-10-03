#!/bin/bash
# Deploy script for Magpie: stop → pull → build → start

set -e  # Exit on any error

LOG_FILE="deploy_$(date +%Y%m%d_%H%M%S).log"

log() {
  echo "[$(date +'%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

error() {
  echo "❌ ERROR: $*" | tee -a "$LOG_FILE"
  exit 1
}

# Check prerequisites
command -v docker &> /dev/null || error "docker not found. Please install Docker."
command -v git &> /dev/null || error "git not found. Please install Git."

log "🛑 Stopping Docker containers..."
sudo docker compose down || error "Failed to stop containers"

log "📥 Pulling latest changes..."
sudo git pull || error "Failed to pull latest changes"

log "🔨 Building Docker image..."
sudo docker compose build || error "Failed to build image"

log "🚀 Starting services..."
sudo docker compose up -d || error "Failed to start services"

log "✅ Deploy complete!"
log "Magpie is running at http://localhost:8000"
log "Logs saved to $LOG_FILE"
sudo docker compose logs -f magpie
