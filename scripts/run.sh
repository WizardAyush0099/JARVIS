#!/bin/sh
# ---------------------------------------------------------------------------
# JARVIS launcher
#
#   sh scripts/run.sh                 # web interface
#   sh scripts/run.sh --gui           # desktop window
#   sh scripts/run.sh --cli           # terminal session
#   sh scripts/run.sh --check         # diagnostics
#
# Uses the project virtual environment when one exists, otherwise the system
# Python. Any extra arguments are passed straight through to main.py.
# ---------------------------------------------------------------------------
set -e

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"

PYTHON=""
for candidate in ".venv/bin/python" "venv/bin/python" "env/bin/python"; do
  if [ -x "$candidate" ]; then
    PYTHON="$candidate"
    break
  fi
done
if [ -z "$PYTHON" ]; then
  PYTHON=$(command -v python3 || true)
fi
[ -n "$PYTHON" ] || { echo "Python 3 was not found"; exit 1; }

exec "$PYTHON" main.py "$@"
