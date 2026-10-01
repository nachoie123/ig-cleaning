#!/bin/bash
# Run from source (developers). Regular users: download the .dmg from Releases.
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv && .venv/bin/pip install -q pywebview certifi
fi
exec .venv/bin/python app.py
