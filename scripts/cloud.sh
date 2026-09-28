#!/usr/bin/env sh
# Start JARVIS inside a cloud dev container - GitHub Codespaces, VS Code Dev
# Containers, or any other headless box you can only reach through a phone.
#
#   sh scripts/cloud.sh          # start in the background, log to logs/cloud.log
#   sh scripts/cloud.sh --stop   # stop it again
#
# Differs from scripts/run.sh in two ways that matter on a remote machine:
# it never tries to open a browser (there isn't one), and running it twice is
# safe, so it can be wired into a container's start-up hook.
set -eu

PORT="${PORT:-${JARVIS_WEB_PORT:-8765}}"
HOST="${JARVIS_WEB_HOST:-0.0.0.0}"
PIDFILE="${JARVIS_CLOUD_PIDFILE:-logs/jarvis.pid}"
LOG="${JARVIS_CLOUD_LOG:-logs/cloud.log}"

PYTHON="${JARVIS_PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    PYTHON="python"
fi

mkdir -p "$(dirname "$PIDFILE")" "$(dirname "$LOG")"

running() {
    [ -f "$PIDFILE" ] || return 1
    pid="$(cat "$PIDFILE" 2>/dev/null || true)"
    [ -n "$pid" ] || return 1
    kill -0 "$pid" 2>/dev/null
}

if [ "${1:-}" = "--stop" ]; then
    if running; then
        kill "$(cat "$PIDFILE")" 2>/dev/null || true
        rm -f "$PIDFILE"
        echo "JARVIS stopped."
    else
        echo "JARVIS is not running."
    fi
    exit 0
fi

if running; then
    echo "JARVIS is already running (pid $(cat "$PIDFILE"))."
    echo "Open the forwarded port $PORT - or read $LOG if it looks wrong."
    exit 0
fi

echo "starting JARVIS on $HOST:$PORT"
echo "  log: $LOG"

# --no-browser: there is no display here.  The console is reached through the
# forwarded port instead.
nohup "$PYTHON" main.py --web --no-browser --host "$HOST" --port "$PORT" >>"$LOG" 2>&1 &
echo $! >"$PIDFILE"

echo "JARVIS started (pid $(cat "$PIDFILE")). Forwarded port: $PORT"
