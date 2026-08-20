#!/bin/bash
# Linux launcher. Same as the .command file, without the Finder niceties.
cd "$(dirname "$0")" || exit 1
[ -d .venv ] || python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -c "import requests" 2>/dev/null || pip install -q -r requirements.txt
exec python app.py
