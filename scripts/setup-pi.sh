#!/bin/sh
# ---------------------------------------------------------------------------
# JARVIS one-command setup for a Raspberry Pi (or any Debian/Ubuntu machine)
#
#   sh scripts/setup-pi.sh                 # venv + core + voice + hardware + dev
#   sh scripts/setup-pi.sh --lean          # venv + core only (smallest install)
#   sh scripts/setup-pi.sh --system        # also apt-get the Pi audio/GPIO packages
#   sh scripts/setup-pi.sh --venv ~/.jarvis-venv
#
# Run it once after cloning, then start JARVIS with:
#   .venv/bin/python main.py            # web console on http://<pi-ip>:8765
# or press F5 in VS Code and pick "JARVIS: web interface".
#
# The script is idempotent: running it again only fills in what is missing and
# never overwrites an existing .env.  Written for `sh` (dash on Raspberry Pi OS).
# ---------------------------------------------------------------------------
set -e

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"

WANT_VOICE=1
WANT_HARDWARE=1
WANT_DEV=1
WANT_SYSTEM=0
VENV_DIR=${JARVIS_VENV:-.venv}

for arg in "$@"; do
  case "$arg" in
    --voice) WANT_VOICE=1 ;;
    --no-voice) WANT_VOICE=0 ;;
    --hardware|--hw) WANT_HARDWARE=1 ;;
    --no-hardware) WANT_HARDWARE=0 ;;
    --dev) WANT_DEV=1 ;;
    --no-dev) WANT_DEV=0 ;;
    --system|--apt) WANT_SYSTEM=1 ;;
    --lean) WANT_VOICE=0; WANT_HARDWARE=0; WANT_DEV=0 ;;
    --venv) shift; VENV_DIR=${1:-.venv} ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg (try --help)"; exit 1 ;;
  esac
done

PY=python3
command -v "$PY" >/dev/null 2>&1 || { echo "Python 3 is required (sudo apt install -y python3 python3-venv python3-pip)"; exit 1; }

echo "== JARVIS setup =="
"$PY" --version
uname -m | sed 's/^/architecture: /'

# --- optional system packages (audio + GPIO) -------------------------------
# These need root, so they are opt-in: without --system we only print the line.
APT_PACKAGES="portaudio19-dev flac mpg123 espeak-ng libsdl2-mixer-2.0-0 python3-dev"
if [ "$WANT_VOICE" = "1" ] || [ "$WANT_HARDWARE" = "1" ]; then
  if [ "$WANT_SYSTEM" = "1" ] && command -v apt-get >/dev/null 2>&1; then
    echo "-> installing system packages (may ask for your password)"
    sudo apt-get update -qq || true
    # shellcheck disable=SC2086
    sudo apt-get install -y $APT_PACKAGES || echo "! some system packages failed - JARVIS still runs without them"
  elif command -v apt-get >/dev/null 2>&1; then
    echo "-> for microphone, speaker and GPIO support run this once:"
    echo "     sudo apt-get install -y $APT_PACKAGES"
  fi
fi

# --- virtual environment ---------------------------------------------------
if [ ! -d "$VENV_DIR" ]; then
  echo "-> creating virtual environment in $VENV_DIR"
  "$PY" -m venv "$VENV_DIR"
fi

PIP="$VENV_DIR/bin/pip"
PYBIN="$VENV_DIR/bin/python"
[ -x "$PIP" ] || { echo "could not find $PIP"; exit 1; }

echo "-> installing core requirements"
"$PIP" install --quiet --upgrade pip
"$PIP" install --quiet -r requirements.txt

if [ "$WANT_VOICE" = "1" ]; then
  echo "-> installing voice requirements"
  if ! "$PIP" install -r requirements-voice.txt; then
    echo "! voice extras did not all install - JARVIS still runs, just without local speech"
  fi
fi

if [ "$WANT_HARDWARE" = "1" ]; then
  echo "-> installing hardware requirements"
  if ! "$PIP" install -r requirements-hardware.txt; then
    echo "! hardware extras did not install - the mock GPIO backend will be used"
  fi
fi

if [ "$WANT_DEV" = "1" ]; then
  echo "-> installing development requirements"
  "$PIP" install --quiet -r requirements-dev.txt
fi

# --- configuration ---------------------------------------------------------
if [ ! -f .env ]; then
  if [ -f env.example ]; then
    cp env.example .env
    echo "-> created .env from env.example"
  fi
else
  echo "-> keeping your existing .env"
fi

echo "-> running diagnostics"
"$PYBIN" main.py --check || true

cat <<EOF

Done.

  1. Add at least one AI provider key to .env      (GEMINI_API_KEY is the quickest free one)
     Optional: set JARVIS_WEB_TOKEN=... if the network is not fully trusted.
  2. Start JARVIS:
         $VENV_DIR/bin/python main.py            # web console + prints your LAN address
     or in VS Code press F5 and choose "JARVIS: web interface".
  3. Open the "network" address it prints (http://<pi-ip>:8765) on your phone,
     or http://localhost:8765 on the Pi itself.
EOF
