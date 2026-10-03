#!/bin/bash
# Deploy script for Magpie: stop → pull → build → start (with optional tagging)

set -e  # Exit on any error
set -o pipefail  # a failing command in `cmd | tee` must fail the script too (e.g. tagging)

LOG_FILE="deploy_$(date +%Y%m%d_%H%M%S).log"
TAG_TYPE=""
TAG_MESSAGE=""
TAG_EXTRA=()   # credential options handed on to tag.sh
SHOW_LOGS=false

show_usage() {
  cat << EOF
Usage: $0 [OPTIONS]

Options:
  --tag-major        Bump major version and tag after deploy
  --tag-minor        Bump minor version and tag after deploy
  --tag-patch        Bump patch version and tag after deploy
  -m, --message MSG  Custom tag message
  Credentials for pushing the tag (read from outside, see ./tag.sh --help):
    -u, --user NAME | --user-secret NAME | --user-cmd CMD
    -s, --secret NAME | --secret-cmd CMD
  --foreground       Show logs in foreground (default: start in background)
  --logs             Alias for --foreground
  -h, --help         Show this help message

Examples:
  $0                              # Deploy in background (default)
  $0 --foreground                 # Deploy and show logs in foreground
  $0 --tag-minor                  # Deploy in background and bump minor version
  $0 --tag-major --logs -m "v2.0" # Deploy, show logs, bump major with message
EOF
}

log() {
  echo "[$(date +'%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

error() {
  echo "❌ ERROR: $*" | tee -a "$LOG_FILE"
  exit 1
}

# Show help if no arguments provided (after first run setup)
if [ $# -eq 0 ] && [ -f "docker-compose.yml" ]; then
  log "ℹ️  No options provided. Running default deploy..."
  log "For help, run: $0 --help"
fi

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
    -u|--user|--user-secret|--user-cmd|-s|--secret|--secret-cmd)
      [ $# -ge 2 ] || error "$1 needs a value"
      TAG_EXTRA+=("$1" "$2")
      shift 2
      ;;
    --foreground|--logs)
      SHOW_LOGS=true
      shift
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
  tag_args+=("${TAG_EXTRA[@]}")

  if bash ./tag.sh "${tag_args[@]}" 2>&1 | tee -a "$LOG_FILE"; then
    log "✅ Deployment and tagging complete!"
  else
    error "Tagging failed after successful deployment"
  fi
else
  log "⏭️  Skipping tagging (use --tag-major/minor/patch to tag)"
fi

log "Logs saved to $LOG_FILE"

# Show logs if requested, otherwise just report background status
if [ "$SHOW_LOGS" = true ]; then
  log "📺 Showing live logs (press Ctrl+C to exit)..."
  sudo docker compose logs -f magpie
else
  log "🎯 Running in background (use 'sudo docker compose logs -f magpie' to view logs)"
  log "✅ Ready to accept requests at http://localhost:8000"
fi
