#!/usr/bin/env bash
# Prepare the existing Bookworm/Python 3.11 installation as user admin.
# This script installs the unit but never enables or starts a service.
set -euo pipefail
umask 077

PROJECT_DIR=/home/admin/shabat_elevator
SYSTEM_PYTHON=/usr/bin/python3.11
SKIP_DEPS=false

case "${1:-}" in
    "") ;;
    --skip-deps) SKIP_DEPS=true ;;
    *) printf 'Usage: bash deploy/raspberrypi-bootstrap.sh [--skip-deps]\n' >&2; exit 2 ;;
esac
if (( $# > 1 )); then
    printf 'Only --skip-deps is accepted.\n' >&2
    exit 2
fi
if [[ "$(id -un)" != admin ]]; then
    printf 'Run this script as admin, without sudo. It requests sudo only for system packages and the unit.\n' >&2
    exit 1
fi
if [[ ! -x "$SYSTEM_PYTHON" ]]; then
    printf 'System Python 3.11 is required at %s. No OS upgrade was attempted.\n' "$SYSTEM_PYTHON" >&2
    exit 1
fi
cd -- "$PROJECT_DIR"
for required in run_metrics_server.py web/config.json data/server-config.json tests/test_server.py tests/test_audio_recorder.py tests/test_live_detector.py deploy/shabat-elevator.service; do
    if [[ ! -f "$required" ]]; then
        printf 'Missing project file: %s/%s\n' "$PROJECT_DIR" "$required" >&2
        exit 1
    fi
done

if [[ ! -d .venv ]]; then
    "$SYSTEM_PYTHON" -m venv --system-site-packages .venv
fi
# Use Debian's ARM NumPy package rather than building it on this small board.
"$SYSTEM_PYTHON" - <<'PY'
from pathlib import Path
path = Path('.venv/pyvenv.cfg')
text = path.read_text()
if 'include-system-site-packages = false' in text:
    path.write_text(text.replace('include-system-site-packages = false', 'include-system-site-packages = true'))
PY
PYTHON="$PROJECT_DIR/.venv/bin/python"
"$PYTHON" -B -c 'import sys; assert sys.version_info[:2] == (3, 11), "The existing .venv must use Python 3.11"'

if [[ "$SKIP_DEPS" == false ]]; then
    for package in libportaudio2 python3-numpy; do
        if [[ "$(dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null || true)" != installed ]]; then
            sudo apt-get install --no-install-recommends -y "$package"
        fi
    done
    "$PYTHON" -m pip --isolated --disable-pip-version-check install \
        --only-binary=:all: \
        --index-url https://pypi.org/simple \
        --extra-index-url https://www.piwheels.org/simple \
        'sounddevice==0.5.6'
fi

# Importing sounddevice verifies that PortAudio can be loaded without recording.
"$PYTHON" -B - <<'PY'
from importlib.metadata import version
import cffi
import sounddevice
import numpy
import json
from pathlib import Path
from elevator.runtime_config import validate_runtime_config

if version("sounddevice") != "0.5.6":
    raise SystemExit("sounddevice 0.5.6 is required; rerun without --skip-deps")
sounddevice.get_portaudio_version()
validate_runtime_config(json.loads(Path('data/server-config.json').read_text()))
print("Python 3.11, NumPy, sounddevice 0.5.6, CFFI, PortAudio and detector configuration are available.")
PY

mkdir -p -- "$PROJECT_DIR/data"
"$PYTHON" -B - <<'PY'
import os
import secrets

try:
    descriptor = os.open(".env", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except FileExistsError:
    print("Existing .env retained.")
else:
    with os.fdopen(descriptor, "w", encoding="utf-8") as target:
        target.write("INGEST_TOKEN=" + secrets.token_urlsafe(48) + "\n")
        target.write("DEVICE_ID=begin17-floor7\n")
        target.write("ALLOWED_ORIGIN=https://udid.github.io\n")
    print("Created private .env (0600); token is not printed.")
PY

# These suites use temporary state and simulated audio; no microphone is opened.
DEVICE_ID=begin17-floor7 "$PYTHON" -B -m unittest discover -s tests -p 'test_server.py' -v
"$PYTHON" -B -m unittest discover -s tests -p 'test_audio_recorder.py' -v
"$PYTHON" -B -m unittest discover -s tests -p 'test_live_detector.py' -v
"$PYTHON" -B -m unittest discover -s tests -p 'test_state.py' -v
"$PYTHON" -B -m unittest discover -s tests -p 'test_detector_service.py' -v
"$PYTHON" -B -m unittest discover -s tests -p 'test_diagnostics.py' -v

sudo install -m 0644 deploy/shabat-elevator.service /etc/systemd/system/shabat-elevator.service
sudo systemctl daemon-reload
printf '\nChecks passed and the unit is installed. No service was enabled or started.\n'
printf 'Start separately when ready: sudo systemctl enable --now shabat-elevator.service\n'
