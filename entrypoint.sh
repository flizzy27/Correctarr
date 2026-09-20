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

umask "${UMASK}"

say() { printf '%s  %-7s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" "$2"; }

say INFO "Correctarr ${VERSION:-dev} starting"

serve() {
  exec uvicorn app.main:app --host "${HOST}" --port "${PORT}" \
       --no-server-header --proxy-headers --forwarded-allow-ips='*'
}

# If the container is not running as root — because someone passed --user, say —
# permissions cannot be changed. Start directly in that case.
if [ "$(id -u)" != "0" ]; then
  say INFO "Running as $(id -u):$(id -g), leaving permissions alone"
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

say INFO "User ${PUID}:${PGID}, timezone ${TZ:-Etc/UTC}, port ${PORT}"

exec gosu "${PUID}:${PGID}" \
     uvicorn app.main:app --host "${HOST}" --port "${PORT}" \
     --no-server-header --proxy-headers --forwarded-allow-ips='*'
