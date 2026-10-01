#!/bin/bash
# Builds "IG Cleaning.app" (universal: Apple Silicon + Intel) into dist/.
set -e
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/pip install -q pywebview certifi pyinstaller
.venv/bin/pyinstaller --clean --noconfirm --windowed --name "IG Cleaning" \
  --icon build_assets/icon.icns --add-data "web/index.html:web" \
  --target-arch universal2 --osx-bundle-identifier com.nachosanbenito.iglimpieza app.py
