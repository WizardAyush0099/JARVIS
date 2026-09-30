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
# portaudio19-dev  -> PyAudio (microphone capture)
# swig             -> lgpio (GPIO backend gpiozero uses on Bookworm)
# build-essential  -> the compiler both of them need
APT_PACKAGES="portaudio19-dev flac mpg123 espeak-ng libsdl2-mixer-2.0-0 python3-dev swig build-essential"
# Raspberry Pi OS packages liblgpio-dev (lgpio links against it); most other
# distributions do not, and apt installs *nothing* when one name is unknown, so
# this one is asked for separately and may be skipped without harm.
APT_PACKAGES_OPTIONAL="liblgpio-dev"
if [ "$WANT_VOICE" = "1" ] || [ "$WANT_HARDWARE" = "1" ]; then
  if [ "$WANT_SYSTEM" = "1" ] && command -v apt-get >/dev/null 2>&1; then
    echo "-> installing system packages (may ask for your password)"
    sudo apt-get update -qq || true
    # shellcheck disable=SC2086
    sudo apt-get install -y $APT_PACKAGES || echo "! some system packages failed - JARVIS still runs without them"
    # shellcheck disable=SC2086
    sudo apt-get install -y $APT_PACKAGES_OPTIONAL || echo "! $APT_PACKAGES_OPTIONAL is not packaged here - only the lgpio build wants it"
  elif command -v apt-get >/dev/null 2>&1; then
    echo "-> for microphone, speaker and GPIO support run this once:"
    echo "     sudo apt-get install -y $APT_PACKAGES"
    echo "     sudo apt-get install -y $APT_PACKAGES_OPTIONAL   # Raspberry Pi OS; skip if unknown"
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

# --- install a requirements file one package at a time ----------------------
# `pip install -r` is all-or-nothing: the moment one line has to be compiled
# from source and the toolchain is incomplete - PyAudio needs PortAudio's
# headers, lgpio needs swig - pip discards the whole batch, including the
# packages that already had ARM wheels waiting (edge-tts, gpiozero, vosk).
# Going line by line means one stubborn package costs only itself, and we can
# name it at the end with the apt line that fixes it.
SKIPPED=""
pip_one_by_one() {
  file=$1
  while IFS= read -r raw || [ -n "$raw" ]; do
    pkg=$(printf '%s' "${raw%%#*}" | sed 's/[[:space:]]*$//')
    [ -n "$pkg" ] || continue
    name=${pkg%%[<>=!]*}
    if "$PIP" install --quiet "$pkg"; then
      echo "   ok      $name"
    else
      echo "   missing $name"
      SKIPPED="$SKIPPED $name"
    fi
  done < "$file"
}

echo "-> installing core requirements"
"$PIP" install --quiet --upgrade pip
"$PIP" install --quiet -r requirements.txt

if [ "$WANT_VOICE" = "1" ]; then
  echo "-> installing voice requirements"
  SKIPPED=""
  pip_one_by_one requirements-voice.txt
  if [ -n "$SKIPPED" ]; then
    echo "! voice packages not installed:$SKIPPED"
    case " $SKIPPED " in
      *" PyAudio "*)
        echo "  PyAudio is the microphone. It compiles against PortAudio's C headers,"
        echo "  so it needs the build tools once. Fix it with:"
        echo "     sudo apt install -y portaudio19-dev python3-dev build-essential"
        echo "     $PIP install PyAudio"
        echo "  or re-run this script with --system. Speech output is unaffected."
        ;;
    esac
    echo "  JARVIS still runs; type instead of talking until then."
  else
    echo "   every voice package installed"
  fi
fi

if [ "$WANT_HARDWARE" = "1" ]; then
  echo "-> installing hardware requirements"
  SKIPPED=""
  pip_one_by_one requirements-hardware.txt
  if [ -n "$SKIPPED" ]; then
    echo "! hardware packages not installed:$SKIPPED"
    case " $SKIPPED " in
      *" lgpio "*)
        echo "  lgpio generates its C bindings with swig and links against lgpio's"
        echo "  C library. Fix it with:"
        echo "     sudo apt-get install -y swig python3-dev build-essential liblgpio-dev"
        echo "     $PIP install lgpio"
        echo "  or re-run this script with --system."
        ;;
    esac
    echo "  gpiozero falls back to another GPIO backend; with none available the"
    echo "  mock backend is used and JARVIS says so when it answers."
  else
    echo "   every hardware package installed"
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
