#!/bin/sh
# Bind-mounted ./data is often root-owned; fix that, then drop privileges.
set -e
if [ "$(id -u)" = "0" ]; then
    mkdir -p "${MAGPIE_DATA_DIR:-/data}"
    chown magpie:magpie "${MAGPIE_DATA_DIR:-/data}" 2>/dev/null || true
    exec setpriv --reuid=magpie --regid=magpie --init-groups "$@"
fi
exec "$@"
