#!/bin/sh
# ---------------------------------------------------------------------------
# JARVIS installer
#
#   sh scripts/install.sh                 # core only
#   sh scripts/install.sh --voice         # + speech in/out
#   sh scripts/install.sh --hardware      # + GPIO / sensors
#   sh scripts/install.sh --dev           # + test tooling
#   sh scripts/install.sh --voice --hardware --dev
#
# Written for `sh` (dash on Raspberry Pi OS), so it stays simple on purpose.
# ---------------------------------------------------------------------------
set -e

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"

WANT_VOICE=0
WANT_HARDWARE=0
WANT_DEV=0
VENV_DIR=${JARVIS_VENV:-.venv}

for arg in "$@"; do
  case "$arg" in
    --voice) WANT_VOICE=1 ;;
    --hardware|--hw) WANT_HARDWARE=1 ;;
    --dev) WANT_DEV=1 ;;
    --venv) shift; VENV_DIR=${1:-.venv} ;;
    -h|--help)
      sed -n '2,14p' "$0"
      exit 0
      ;;
    *)
      echo "unknown option: $arg (try --help)"
      exit 1
      ;;
  esac
done

PY=python3
command -v "$PY" >/dev/null 2>&1 || { echo "Python 3 is required"; exit 1; }

echo "== JARVIS installer =="
"$PY" --version

if [ ! -d "$VENV_DIR" ]; then
  echo "-> creating virtual environment in $VENV_DIR"
  "$PY" -m venv "$VENV_DIR"
fi

PIP="$VENV_DIR/bin/pip"
PYBIN="$VENV_DIR/bin/python"
[ -x "$PIP" ] || { echo "could not find $PIP"; exit 1; }

echo "-> upgrading pip"
"$PIP" install --quiet --upgrade pip

echo "-> installing core requirements"
"$PIP" install --quiet -r requirements.txt

if [ "$WANT_VOICE" = "1" ]; then
  echo "-> installing voice requirements"
  echo "   (if PyAudio fails, run: sudo apt install -y portaudio19-dev flac mpg123 espeak-ng)"
  if ! "$PIP" install -r requirements-voice.txt; then
    echo "! voice extras did not all install - JARVIS will still work without them"
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

if [ ! -f .env ]; then
  if [ -f env.example ]; then
    cp env.example .env
    echo "-> created .env from env.example (add your API keys to it)"
  fi
else
  echo "-> keeping your existing .env"
fi

echo "-> running diagnostics"
"$PYBIN" main.py --check || true

cat <<'EOF'

Done.

  1. put at least one AI provider key in .env   (see the comments in the file)
  2. start JARVIS:
         .venv/bin/python main.py            # web interface
         .venv/bin/python main.py --gui      # desktop window
         .venv/bin/python main.py --cli      # terminal only
  3. open the LAN address it prints from your phone
EOF
