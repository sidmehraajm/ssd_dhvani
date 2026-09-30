#!/usr/bin/env bash
# ==========================================================================
#  build.sh  --  build ssd_dhvani_v<major>.app with PyInstaller (macOS)
#
#  Run from the project root (where main.py lives):
#      ./build.sh           normal build
#      ./build.sh debug     keeps a console (tracebacks) -- run the binary
#                           inside the .app from Terminal to see them
#
#  Produces:  dist/ssd_dhvani_v1.app  (+ dist/config.json next to it)
#  First launch: right-click -> Open (the app is not notarised), then allow
#  the microphone when macOS asks.
# ==========================================================================
set -euo pipefail
cd "$(dirname "$0")"

[[ "${1:-}" == "debug" ]] && export SSD_BUILD_CONSOLE=1

echo "=== ssd_dhvani build (macOS) ==="
if [[ ! -x .venv/bin/python ]]; then
    echo "[1/5] Creating virtual environment .venv ..."
    python3 -m venv .venv
else
    echo "[1/5] Reusing existing .venv"
fi
# shellcheck disable=SC1091
source .venv/bin/activate

echo "[2/5] Installing dependencies ..."
python -m pip install --upgrade pip
python -m pip install -r requirements.txt pyinstaller

echo "[3/5] Cleaning old build/ and dist/ ..."
rm -rf build dist

echo "[4/5] Running PyInstaller ..."
pyinstaller ssd_dhvani.spec --noconfirm --clean

echo "[5/5] Staging config.json next to the .app ..."
if [[ ! -f dist/config.json ]]; then
    if [[ -f config.json ]]; then cp config.json dist/config.json
    else cp config.example.json dist/config.json; fi
fi
APP=$(ls -d dist/*.app | head -1)
echo
echo "=== DONE ==="
echo "  App:     $APP"
echo "  Config:  dist/config.json  (AYON server / save root)"
