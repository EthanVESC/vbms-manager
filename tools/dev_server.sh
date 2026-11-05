#!/bin/bash
# Development instance: two simulated boards and VBMS Manager on a spare port, with a throwaway data folder.
# Leaves a normal VBMS Manager (port 8642, ~/.vbms_manager) running untouched.
#
#   tools/dev_server.sh              # http://127.0.0.1:8650/
#   VBMS_DEV_PORT=8651 VBMS_DEV_HOME=/tmp/x tools/dev_server.sh
#   VBMS_DEV_FRESH=1 tools/dev_server.sh    # start from an empty data folder (first-run flows)
#
# Talks to the mock boards only (tests/mock_bms.py). Never add a real board here.
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${VBMS_DEV_PORT:-8650}"
HOME_DIR="${VBMS_DEV_HOME:-${TMPDIR:-/tmp}/vbms-dev-home}"
PY="${PYTHON:-python3}"
if [ "$PORT" = 8642 ]; then echo "Pick a port other than 8642 (that's the normal VBMS Manager)." >&2; exit 1; fi
case "$HOME_DIR" in "$HOME/.vbms_manager"*) echo "Refusing to use the real data folder." >&2; exit 1;; esac

if [ -n "${VBMS_DEV_FRESH:-}" ] && [ -d "$HOME_DIR" ]; then rm -rf "$HOME_DIR"; fi
export VBMS_MANAGER_HOME="$HOME_DIR"
mkdir -p "$HOME_DIR"

pids=""
cleanup() { for p in $pids; do kill "$p" 2>/dev/null || true; done; }
trap cleanup EXIT INT TERM

"$PY" -m tests.mock_bms vbms32_32s 65199 --sim & pids="$pids $!"
"$PY" -m tests.mock_bms vbms32_18s 65198 --sim & pids="$pids $!"

# Seed a month of history once, so the History tab has something to show.
if [ ! -e "$HOME_DIR/.seeded" ]; then
  "$PY" tools/seed_history.py 02:00:00:00:fe:af --days 30 --cells 32 >/dev/null
  "$PY" tools/seed_history.py 02:00:00:00:fe:ae --days 30 --cells 18 >/dev/null
  touch "$HOME_DIR/.seeded"
fi

echo "VBMS Manager dev: data in $HOME_DIR"
"$PY" vbms_manager.py --port "$PORT" --no-browser --no-discovery \
  --add 127.0.0.1:65199 --add 127.0.0.1:65198 & server=$!
pids="$pids $server"
wait "$server"
