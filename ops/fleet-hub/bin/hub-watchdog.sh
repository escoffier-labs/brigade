#!/bin/sh
# Host watchdog for the Python-only recipe, run every 10 s by
# systemd/brigade-hub-watchdog.timer until `fleet serve --engine rust` (S10)
# carries the same rules inside the container. Docker does not restart an
# unhealthy container, so this restarts the hub container after LIMIT
# consecutive checks in which /health fails, or after LIMIT consecutive
# checks that find no network interface but loopback in the hub's namespace
# (the holder restarted and the hub kept the namespace it had joined).
set -u

PROJECT_DIR="${BRIGADE_HUB_PROJECT_DIR:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)}"
STATE_DIR="${RUNTIME_DIRECTORY:-${BRIGADE_WATCHDOG_STATE_DIR:-/run/brigade-hub-watchdog}}"
LIMIT="${BRIGADE_WATCHDOG_FAILURES:-3}"
DOCKER="${BRIGADE_DOCKER:-docker}"

# Exit 0 healthy, 10 /health failed, 11 only loopback, 12 both.
PROBE='
import os, sys, urllib.request
lonely = all(name == "lo" for name in os.listdir("/sys/class/net"))
try:
    urllib.request.urlopen("http://127.0.0.1:3774/health", timeout=2).read()
    sick = False
except Exception:
    sick = True
sys.exit(12 if sick and lonely else 10 if sick else 11 if lonely else 0)
'

mkdir -p "$STATE_DIR"
count() { cat "$STATE_DIR/$1" 2>/dev/null || echo 0; }
store() { printf '%s\n' "$2" > "$STATE_DIR/$1"; }

container="$($DOCKER compose --project-directory "$PROJECT_DIR" ps -q hub 2>/dev/null)"
if [ -z "$container" ] || [ "$($DOCKER inspect -f '{{.State.Running}}' "$container" 2>/dev/null)" != true ]; then
  # Not running: the restart policy and the boot unit own that case.
  store health 0
  store netns 0
  exit 0
fi

$DOCKER exec "$container" python -c "$PROBE" >/dev/null 2>&1
rc=$?
health="$(count health)"
netns="$(count netns)"
case "$rc" in
  0) health=0; netns=0 ;;
  10) health=$((health + 1)); netns=0 ;;
  11) health=0; netns=$((netns + 1)) ;;
  12) health=$((health + 1)); netns=$((netns + 1)) ;;
  *) health=$((health + 1)) ;;  # exec itself failed: a hung or broken container
esac
store health "$health"
store netns "$netns"

reason=""
if [ "$netns" -ge "$LIMIT" ]; then
  reason="no network interface but loopback in $LIMIT checks"
elif [ "$health" -ge "$LIMIT" ]; then
  reason="/health failed $LIMIT checks in a row"
fi
if [ -n "$reason" ]; then
  echo "brigade-hub-watchdog: $reason; restarting the hub container" >&2
  store health 0
  store netns 0
  # Exit 0 either way, so a restart never marks the unit failed and the
  # timer keeps its 10 s cadence.
  $DOCKER restart -t 40 "$container" >/dev/null || echo "brigade-hub-watchdog: docker restart failed" >&2
fi
exit 0
