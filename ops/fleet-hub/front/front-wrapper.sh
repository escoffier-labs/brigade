#!/bin/sh
# Entrypoint for the HTTPS front, which runs in the holder container's network
# namespace beside the hub. A container that joined another container's
# namespace keeps it after the owner restarts and is left with loopback only,
# so this wrapper watches its own namespace and exits after LIMIT checks,
# INTERVAL seconds apart, that find no interface but loopback, or LIMIT checks
# in which the hub port refuses connections. The restart policy then starts
# the front again in the holder's current namespace.
set -u

INTERVAL="${BRIGADE_NETNS_CHECK_INTERVAL:-10}"
LIMIT="${BRIGADE_NETNS_CHECK_FAILURES:-3}"
NET_DIR="${BRIGADE_NET_DIR:-/sys/class/net}"
HUB_URL="${BRIGADE_HUB_HEALTH_URL:-http://127.0.0.1:3774/health}"
FRONT_CMD="${BRIGADE_FRONT_CMD:-/usr/local/bin/containerboot}"

if [ -z "${TS_AUTHKEY:-}" ] && [ -n "${BRIGADE_FRONT_AUTHKEY_FILE:-}" ] && [ -s "${BRIGADE_FRONT_AUTHKEY_FILE}" ]; then
  TS_AUTHKEY="$(cat "${BRIGADE_FRONT_AUTHKEY_FILE}")"
  export TS_AUTHKEY
fi

only_loopback() {
  for path in "$NET_DIR"/*; do
    [ -e "$path" ] || continue
    [ "${path##*/}" = lo ] || return 1
  done
  return 0
}

hub_refuses() {
  # Only a refused connection counts. A slow or failing hub is the hub
  # watchdog's business, not a reason to leave the namespace.
  output="$(wget -T 3 -O /dev/null "$HUB_URL" 2>&1)" && return 1
  case "$output" in
    *[Rr]efused*) return 0 ;;
    *) return 1 ;;
  esac
}

$FRONT_CMD &
child=$!
trap 'kill -TERM "$child" 2>/dev/null; wait "$child"; exit $?' TERM INT

netns_failures=0
hub_failures=0
while kill -0 "$child" 2>/dev/null; do
  sleep "$INTERVAL" &
  wait $!
  if only_loopback; then netns_failures=$((netns_failures + 1)); else netns_failures=0; fi
  if hub_refuses; then hub_failures=$((hub_failures + 1)); else hub_failures=0; fi
  reason=""
  if [ "$netns_failures" -ge "$LIMIT" ]; then
    reason="no network interface but loopback after $LIMIT checks"
  elif [ "$hub_failures" -ge "$LIMIT" ]; then
    reason="the hub port refused connections in $LIMIT checks"
  fi
  if [ -n "$reason" ]; then
    echo "front-wrapper: $reason; exiting so the restart policy rejoins the holder's namespace" >&2
    kill -TERM "$child" 2>/dev/null
    wait "$child"
    exit 70
  fi
done
wait "$child"
exit $?
