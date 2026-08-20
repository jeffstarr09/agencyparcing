#!/bin/bash
# Double-click this file in Finder to start the app.
#
# The first run takes a minute while it sets itself up. After that it's quick.
# Leave the black Terminal window open while you use the app; closing it quits.

cd "$(dirname "$0")" || exit 1

echo ""
echo "  TikTok Gap"
echo "  ----------"
echo ""

# Find a Python 3. macOS ships one, but python.org's is more reliable for this.
PY=""
for candidate in python3 /usr/local/bin/python3 /opt/homebrew/bin/python3 /usr/bin/python3; do
  if command -v "$candidate" >/dev/null 2>&1; then PY="$candidate"; break; fi
done

if [ -z "$PY" ]; then
  echo "  Python 3 isn't installed."
  echo ""
  echo "  Get it from  https://www.python.org/downloads/"
  echo "  Install it, then double-click this file again."
  echo ""
  read -r -p "  Press Return to close. "
  exit 1
fi

# A private folder for this app's packages, so nothing else on your Mac changes.
if [ ! -d ".venv" ]; then
  echo "  First run — setting up (about a minute)..."
  "$PY" -m venv .venv || { echo "  Could not create the environment."; read -r; exit 1; }
fi

# shellcheck disable=SC1091
source .venv/bin/activate

if ! python -c "import requests" >/dev/null 2>&1; then
  echo "  Installing what it needs..."
  python -m pip install --quiet --upgrade pip
  python -m pip install --quiet -r requirements.txt || {
    echo "  Could not install the packages. Are you online?"
    read -r -p "  Press Return to close. "
    exit 1
  }
fi

echo "  Starting. Your browser should open in a second."
echo "  Keep this window open while you use it."
echo ""

python app.py

echo ""
read -r -p "  The app has stopped. Press Return to close this window. "
