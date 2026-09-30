#!/bin/sh
# Container entrypoint.
#
# Job: get the permissions right, then start the service as the requested user.
# On Unraid everything under /mnt/user belongs to 99:100 (nobody:users); on
# other systems 1000:1000 is usual. That is why PUID and PGID are configurable
# rather than hard wired.
set -eu

PUID="${PUID:-99}"
PGID="${PGID:-100}"
UMASK="${UMASK:-022}"
PORT="${PORT:-8099}"
HOST="${HOST:-0.0.0.0}"
CONFIG_DIR="${CONFIG_DIR:-/config}"
# Whose X-Forwarded-For and X-Forwarded-Proto are believed. "*" is what makes a
# reverse proxy work without further setup; naming the proxy's address instead
# stops anyone else from choosing the address the sign-in throttle sees.
FORWARDED_ALLOW_IPS="${FORWARDED_ALLOW_IPS:-*}"
STORE="${CONFIG_DIR}/correctarr.db"

umask "${UMASK}"

say() { printf '%s  %-7s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" "$2"; }

say INFO "Correctarr ${VERSION:-dev} starting"

# The store holds every API key and token, so it is readable by its owner
# only. The folder itself keeps its permissions: on Unraid the appdata share is
# opened over the network, and a folder nobody else may enter looks broken.
#
# On a fresh install the file is created here, empty, so it never exists with
# the default mode — SQLite takes an empty file as an empty database. Not while
# a store under the earlier name is waiting to be adopted: its existence is what
# the adoption checks for.
protect_store() {
  if [ ! -e "${STORE}" ] && [ ! -e "${CONFIG_DIR}/radarr-fixer.db" ]; then
    : > "${STORE}" 2>/dev/null || true
  fi
  for file in "${STORE}" "${STORE}-wal" "${STORE}-shm" "${CONFIG_DIR}"/*.backup; do
    if [ -e "${file}" ]; then
      chmod 600 "${file}" 2>/dev/null || true
    fi
  done
}

serve() {
  exec "$@" uvicorn app.main:app --host "${HOST}" --port "${PORT}" \
       --no-server-header --proxy-headers \
       --forwarded-allow-ips="${FORWARDED_ALLOW_IPS}"
}

# If the container is not running as root — because someone passed --user, say —
# permissions cannot be changed. Start directly in that case.
if [ "$(id -u)" != "0" ]; then
  say INFO "Running as $(id -u):$(id -g), leaving permissions alone"
  mkdir -p "${CONFIG_DIR}" 2>/dev/null || true
  protect_store
  serve
fi

# Move the group and user onto the requested ids.
if [ "$(id -g correctarr 2>/dev/null || echo '')" != "${PGID}" ]; then
  groupmod -o -g "${PGID}" correctarr 2>/dev/null || true
fi
if [ "$(id -u correctarr 2>/dev/null || echo '')" != "${PUID}" ]; then
  usermod -o -u "${PUID}" correctarr 2>/dev/null || true
fi

mkdir -p "${CONFIG_DIR}"

# Only the configuration directory is taken over. The mounted media folders are
# left alone — a recursive chown across a film library would be unforgivable:
# it would run for minutes and change permissions nobody asked to change.
if [ "$(stat -c '%u:%g' "${CONFIG_DIR}")" != "${PUID}:${PGID}" ]; then
  say INFO "Setting ownership of ${CONFIG_DIR} to ${PUID}:${PGID}"
  chown -R "${PUID}:${PGID}" "${CONFIG_DIR}" || \
    say WARNING "Could not take ownership of ${CONFIG_DIR} — carrying on anyway"
fi

protect_store
# Created by root a moment ago, and it has to belong to whoever runs the service.
if [ -e "${STORE}" ]; then
  chown "${PUID}:${PGID}" "${STORE}" 2>/dev/null || true
fi

say INFO "User ${PUID}:${PGID}, timezone ${TZ:-Etc/UTC}, port ${PORT}"

serve gosu "${PUID}:${PGID}"
