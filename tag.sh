#!/bin/bash
# Tagging script for Magpie: manage semantic version tags

set -e

log() {
  echo "[$(date +'%Y-%m-%d %H:%M:%S')] $*"
}

error() {
  echo "❌ ERROR: $*" >&2
  exit 1
}

show_usage() {
  cat << EOF
Usage: $0 [OPTIONS] COMMAND

Commands:
  major       Bump major version (e.g., 1.0.0 → 2.0.0)
  minor       Bump minor version (e.g., 1.0.0 → 1.1.0)
  patch       Bump patch version (e.g., 1.0.0 → 1.0.1) [default]
  show        Show current version
  list        List last 10 tags

Options:
  -m, --message MESSAGE   Custom tag message
  -s, --secret NAME       Retrieve GitHub token from secret NAME (default: GITHUB_TOKEN)
  --no-push              Don't push tags to remote
  -h, --help             Show this help message

Examples:
  $0 major                    # Bump to next major version
  $0 -m "Release v2.0" minor  # Bump minor with custom message
  $0 -s GH_TOKEN patch        # Bump patch using token from GH_TOKEN secret
EOF
}

# Defaults
COMMAND="patch"
MESSAGE=""
SECRET_NAME="GITHUB_TOKEN"
PUSH_TAGS=true

# Parse options
while [[ $# -gt 0 ]]; do
  case $1 in
    -m|--message)
      MESSAGE="$2"
      shift 2
      ;;
    -s|--secret)
      SECRET_NAME="$2"
      shift 2
      ;;
    --no-push)
      PUSH_TAGS=false
      shift
      ;;
    -h|--help)
      show_usage
      exit 0
      ;;
    major|minor|patch|show|list)
      COMMAND="$1"
      shift
      ;;
    *)
      error "Unknown option: $1"
      ;;
  esac
done

# Check prerequisites
command -v git &> /dev/null || error "git not found"

# Get current version from latest tag
get_current_version() {
  git describe --tags --abbrev=0 2>/dev/null || echo "0.0.0"
}

# Parse semantic version
parse_version() {
  local version="$1"
  version="${version#v}"  # Remove 'v' prefix if present
  IFS='.' read -r major minor patch <<< "$version"
  echo "${major:-0} ${minor:-0} ${patch:-0}"
}

# Bump version
bump_version() {
  local version="$1"
  local bump_type="$2"

  read -r major minor patch <<< "$(parse_version "$version")"

  case $bump_type in
    major)
      ((major++))
      minor=0
      patch=0
      ;;
    minor)
      ((minor++))
      patch=0
      ;;
    patch)
      ((patch++))
      ;;
  esac

  echo "v${major}.${minor}.${patch}"
}

# Main logic
case $COMMAND in
  show)
    current=$(get_current_version)
    log "Current version: $current"
    ;;

  list)
    log "Last 10 tags:"
    git tag -l --sort=-version:refname | head -10
    ;;

  major|minor|patch)
    current=$(get_current_version)
    new_version=$(bump_version "$current" "$COMMAND")

    log "Bumping $COMMAND: $current → $new_version"

    # Get token from secret/environment
    token="${!SECRET_NAME}"
    if [ -z "$token" ] && [ -f "$HOME/.secrets/$SECRET_NAME" ]; then
      token=$(cat "$HOME/.secrets/$SECRET_NAME")
    fi

    # Use token if available for authentication (optional)
    if [ -n "$token" ]; then
      export GIT_ASKPASS_OVERRIDE=1
      log "Using token from $SECRET_NAME for authentication"
    fi

    # Create tag
    tag_message="${MESSAGE:-Release $new_version}"
    log "Creating tag: $new_version"
    git tag -a "$new_version" -m "$tag_message"

    if [ "$PUSH_TAGS" = true ]; then
      log "🚀 Pushing tag to remote..."
      git push -u origin "$new_version" || error "Failed to push tag"
      log "✅ Tag pushed successfully"
    else
      log "⏭️  Tag created locally (not pushed)"
      log "To push later, run: git push -u origin $new_version"
    fi

    log "✅ Tagging complete!"
    ;;

  *)
    error "Unknown command: $COMMAND"
    ;;
esac
