#!/bin/bash
# Deploy script for Magpie: stop → pull → build → start (with optional tagging)

set -e  # Exit on any error

LOG_FILE="deploy_$(date +%Y%m%d_%H%M%S).log"
TAG_TYPE=""
TAG_MESSAGE=""

show_usage() {
  cat << EOF
Usage: $0 [OPTIONS]

Options:
  --tag-major        Bump major version and tag after deploy
  --tag-minor        Bump minor version and tag after deploy
  --tag-patch        Bump patch version and tag after deploy
  -m, --message MSG  Custom tag message
  -h, --help         Show this help message

Examples:
  $0                              # Deploy without tagging
  $0 --tag-minor                  # Deploy and bump minor version
  $0 --tag-major -m "Major Release"  # Deploy, bump major, with custom message
EOF
}

log() {
  echo "[$(date +'%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

error() {
  echo "❌ ERROR: $*" | tee -a "$LOG_FILE"
  exit 1
}

# Parse options
while [[ $# -gt 0 ]]; do
  case $1 in
    --tag-major)
      TAG_TYPE="major"
      shift
      ;;
    --tag-minor)
      TAG_TYPE="minor"
      shift
      ;;
    --tag-patch)
      TAG_TYPE="patch"
      shift
      ;;
    -m|--message)
      TAG_MESSAGE="$2"
      shift 2
      ;;
    -h|--help)
      show_usage
      exit 0
      ;;
    *)
      error "Unknown option: $1"
      ;;
  esac
done

# Check prerequisites
command -v docker &> /dev/null || error "docker not found. Please install Docker."
command -v git &> /dev/null || error "git not found. Please install Git."
[ -f "./tag.sh" ] && command -v bash &> /dev/null || error "tag.sh not found or bash not available."

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

# Handle tagging if requested
if [ -n "$TAG_TYPE" ]; then
  log "🏷️  Tagging deployment..."

  tag_args=("$TAG_TYPE")
  if [ -n "$TAG_MESSAGE" ]; then
    tag_args+=("-m" "$TAG_MESSAGE")
  fi

  if bash ./tag.sh "${tag_args[@]}" 2>&1 | tee -a "$LOG_FILE"; then
    log "✅ Deployment and tagging complete!"
  else
    error "Tagging failed after successful deployment"
  fi
else
  log "⏭️  Skipping tagging (use --tag-major/minor/patch to tag)"
fi

log "Logs saved to $LOG_FILE"
sudo docker compose logs -f magpie
