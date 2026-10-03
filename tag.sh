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
  -s, --secret NAME       Token for pushing, from the variable NAME or ~/.secrets/NAME (default: GITHUB_TOKEN)
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
  git tag -l 'v[0-9]*' --sort=-version:refname | head -1 | grep . || echo "0.0.0"
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
      major=$((major + 1))
      minor=0
      patch=0
      ;;
    minor)
      minor=$((minor + 1))
      patch=0
      ;;
    patch)
      patch=$((patch + 1))
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
    git rev-parse --git-dir > /dev/null 2>&1 || error "Not inside a git repository"

    # Tags other machines pushed count too, or the same version could be created twice
    git fetch --tags --quiet origin 2>/dev/null || log "⚠️  Couldn't fetch tags from origin; using the local ones"

    current=$(get_current_version)
    new_version=$(bump_version "$current" "$COMMAND")
    git rev-parse -q --verify "refs/tags/$new_version" > /dev/null && error "Tag $new_version already exists"

    # The tag is pushed to the remote, so the commit it marks has to be there too
    if [ "$PUSH_TAGS" = true ] && [ -z "$(git branch -r --contains HEAD 2>/dev/null)" ]; then
      error "HEAD isn't on the remote yet. Push your commits first, or use --no-push."
    fi

    log "Bumping $COMMAND: $current → $new_version"

    # Token from the environment or ~/.secrets/NAME; only used for the push, never printed or put on a command line
    token="${!SECRET_NAME}"
    if [ -z "$token" ] && [ -f "$HOME/.secrets/$SECRET_NAME" ]; then
      token=$(cat "$HOME/.secrets/$SECRET_NAME")
    fi

    tag_message="${MESSAGE:-Release $new_version}"
    log "Creating tag: $new_version"
    git tag -a "$new_version" -m "$tag_message"

    if [ "$PUSH_TAGS" = true ]; then
      log "🚀 Pushing $new_version to origin..."
      push_ok=true
      if [ -n "$token" ]; then
        log "Using the token from $SECRET_NAME"
        askpass=$(mktemp)
        chmod 700 "$askpass"
        cat > "$askpass" << 'ASKPASS'
#!/bin/sh
case "$1" in
  Username*) echo "x-access-token" ;;
  *) echo "$TAG_PUSH_TOKEN" ;;
esac
ASKPASS
        TAG_PUSH_TOKEN="$token" GIT_ASKPASS="$askpass" GIT_TERMINAL_PROMPT=0 git push origin "refs/tags/$new_version" || push_ok=false
        rm -f "$askpass"
      else
        git push origin "refs/tags/$new_version" || push_ok=false
      fi

      if [ "$push_ok" = true ] && git ls-remote --exit-code --tags origin "refs/tags/$new_version" > /dev/null 2>&1; then
        log "✅ Tag $new_version is on the remote"
      else
        git tag -d "$new_version" > /dev/null
        error "Couldn't push $new_version to origin, so the local tag was removed too. Check your access (set $SECRET_NAME or ~/.secrets/$SECRET_NAME to a token with push rights) and try again."
      fi
    else
      log "⏭️  Tag created locally (not pushed)"
      log "To push it later: git push origin refs/tags/$new_version"
    fi

    log "✅ Tagging complete!"
    ;;

  *)
    error "Unknown command: $COMMAND"
    ;;
esac
