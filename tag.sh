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
  --no-push               Don't push the tag to the remote
  -h, --help              Show this help message

Credentials for pushing over https. Git is answered with a user name and a password (a personal access token), taken
from outside the script, never from the command line, and never printed:
  -u, --user NAME         The user name itself (not a secret)
      --user-secret NAME  Where to find the user name (default: GIT_USERNAME); falls back to x-access-token
      --user-cmd CMD      A command that prints the user name (or set TAG_USER_CMD)
  -s, --secret NAME       Where to find the token (default: GITHUB_TOKEN)
      --secret-cmd CMD    A command that prints the token, e.g. a secret manager (or set TAG_SECRET_CMD)

A named secret is looked up, in order, in: the environment variable NAME, the file named by NAME_FILE, a Docker/Kubernetes
secret at /run/secrets/NAME, and the file ~/.secrets/NAME. With no token found, git uses its own credentials.

Examples:
  $0 major                                   # Bump to the next major version and push the tag
  $0 -m "Release v2.0" minor                 # Custom tag message
  $0 -s GH_TOKEN patch                       # Token from \$GH_TOKEN, \$GH_TOKEN_FILE or ~/.secrets/GH_TOKEN
  $0 --user me --secret-cmd "pass show git/github" patch
  $0 --user-cmd "op read op://dev/github/username" --secret-cmd "op read op://dev/github/token" minor
EOF
}

# Defaults
COMMAND="patch"
MESSAGE=""
SECRET_NAME="GITHUB_TOKEN"
USER_SECRET="GIT_USERNAME"
USER_OPT=""
USER_CMD="${TAG_USER_CMD:-}"
SECRET_CMD="${TAG_SECRET_CMD:-}"
PUSH_TAGS=true

need_value() { [ "$1" -ge 2 ] || error "$2 needs a value"; }

# Parse options
while [[ $# -gt 0 ]]; do
  case $1 in
    -m|--message)
      need_value $# "$1"; MESSAGE="$2"
      shift 2
      ;;
    -s|--secret)
      need_value $# "$1"; SECRET_NAME="$2"
      shift 2
      ;;
    --user-secret)
      need_value $# "$1"; USER_SECRET="$2"
      shift 2
      ;;
    -u|--user)
      need_value $# "$1"; USER_OPT="$2"
      shift 2
      ;;
    --user-cmd)
      need_value $# "$1"; USER_CMD="$2"
      shift 2
      ;;
    --secret-cmd)
      need_value $# "$1"; SECRET_CMD="$2"
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

# ---- credentials: answered to git's user name / password prompts, from outside the script -----------------------

valid_name() { [[ "$1" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; }

# Sets REPLY (the value) and REPLY_SOURCE (where it came from; never the value) for the secret called $1:
# $NAME, the file named by $NAME_FILE, a Docker/Kubernetes secret, or ~/.secrets/NAME.
lookup_secret() {
  local name="$1" file_var="${1}_FILE" file
  valid_name "$name" || error "'$name' isn't a usable secret name (letters, digits, underscore)"
  REPLY=""; REPLY_SOURCE=""
  if [ -n "${!name:-}" ]; then
    REPLY="${!name}"; REPLY_SOURCE="environment variable $name"
  elif [ -n "${!file_var:-}" ]; then
    file="${!file_var}"
    [ -r "$file" ] || error "$file_var points to $file, which can't be read"
    REPLY=$(<"$file"); REPLY_SOURCE="file $file (from $file_var)"
  else
    for file in "/run/secrets/$name" "$HOME/.secrets/$name"; do
      if [ -r "$file" ]; then REPLY=$(<"$file"); REPLY_SOURCE="file $file"; break; fi
    done
  fi
  REPLY="${REPLY%$'\r'}"
}

# Sets REPLY from a command's output (a secret manager, `gh auth token`, ...).
run_secret_command() {
  REPLY=$(bash -c "$1") || error "The $2 command failed"
  REPLY="${REPLY%$'\r'}"
  REPLY_SOURCE="command given for the $2"
}

GIT_USER=""; GIT_PASS=""
resolve_credentials() {
  local user_source="" pass_source=""
  if [ -n "$USER_OPT" ]; then
    GIT_USER="$USER_OPT"; user_source="--user"
  elif [ -n "$USER_CMD" ]; then
    run_secret_command "$USER_CMD" "user name"; GIT_USER="$REPLY"; user_source="$REPLY_SOURCE"
  else
    lookup_secret "$USER_SECRET"; GIT_USER="$REPLY"; user_source="$REPLY_SOURCE"
  fi
  if [ -n "$SECRET_CMD" ]; then
    run_secret_command "$SECRET_CMD" "token"; GIT_PASS="$REPLY"; pass_source="$REPLY_SOURCE"
  else
    lookup_secret "$SECRET_NAME"; GIT_PASS="$REPLY"; pass_source="$REPLY_SOURCE"
  fi

  if [ -z "$GIT_PASS" ]; then
    GIT_USER=""
    log "No token found ($SECRET_NAME); git will use its own credentials"
    return 0
  fi
  if [ -z "$GIT_USER" ]; then GIT_USER="x-access-token"; user_source="default"; fi
  log "Git user from ${user_source:-default}; token from $pass_source"

  local scheme; scheme=$(git remote get-url origin 2>/dev/null | sed -n 's#^\([a-z+]*\)://.*#\1#p')
  case "${scheme:-}" in
    https) ;;
    http) log "⚠️  origin is plain http: the token is sent unencrypted" ;;
    *) log "⚠️  origin isn't an http(s) remote, so this user name and token won't be used" ;;
  esac
}

# A throwaway helper git runs when it asks for the user name / password. It reads them from the environment of
# the one git command, so neither appears on a command line or in the process list.
ASKPASS_FILE=""
cleanup() { if [ -n "$ASKPASS_FILE" ]; then rm -f "$ASKPASS_FILE"; fi; }
trap cleanup EXIT

setup_askpass() {
  [ -n "$GIT_PASS" ] || return 0
  ASKPASS_FILE=$(mktemp)
  chmod 700 "$ASKPASS_FILE"
  cat > "$ASKPASS_FILE" << 'ASKPASS'
#!/bin/sh
case "$1" in
  Username*) printf '%s\n' "$TAG_GIT_USER" ;;
  Password*) printf '%s\n' "$TAG_GIT_PASS" ;;
  *) exit 1 ;;
esac
ASKPASS
}

# git, answering its credential prompts when we have credentials (and ignoring any stored ones, which may be stale)
git_auth() {
  if [ -n "$ASKPASS_FILE" ]; then
    TAG_GIT_USER="$GIT_USER" TAG_GIT_PASS="$GIT_PASS" GIT_ASKPASS="$ASKPASS_FILE" GIT_TERMINAL_PROMPT=0 \
      git -c credential.helper= "$@"
  else
    git "$@"
  fi
}

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

    resolve_credentials
    setup_askpass

    # Tags other machines pushed count too, or the same version could be created twice
    git_auth fetch --tags --quiet origin 2>/dev/null || log "⚠️  Couldn't fetch tags from origin; using the local ones"

    current=$(get_current_version)
    new_version=$(bump_version "$current" "$COMMAND")
    git rev-parse -q --verify "refs/tags/$new_version" > /dev/null && error "Tag $new_version already exists"

    # The tag is pushed to the remote, so the commit it marks has to be there too
    if [ "$PUSH_TAGS" = true ] && [ -z "$(git branch -r --contains HEAD 2>/dev/null)" ]; then
      error "HEAD isn't on the remote yet. Push your commits first, or use --no-push."
    fi

    log "Bumping $COMMAND: $current → $new_version"

    tag_message="${MESSAGE:-Release $new_version}"
    log "Creating tag: $new_version"
    git tag -a "$new_version" -m "$tag_message"

    if [ "$PUSH_TAGS" = true ]; then
      log "🚀 Pushing $new_version to origin..."
      push_ok=true
      git_auth push origin "refs/tags/$new_version" || push_ok=false

      if [ "$push_ok" = true ] && git_auth ls-remote --exit-code --tags origin "refs/tags/$new_version" > /dev/null 2>&1; then
        log "✅ Tag $new_version is on the remote"
      else
        git tag -d "$new_version" > /dev/null
        error "Couldn't push $new_version to origin, so the local tag was removed too. Check the git user and token ($SECRET_NAME, see --help for where they are read from) and that the token can push, then try again."
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
