#!/usr/bin/env bash
# Build the standalone app for the current OS (macOS: dist/PDF Combine.app).
set -euo pipefail
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/python -m pip --version >/dev/null 2>&1 || .venv/bin/python -m ensurepip >/dev/null
.venv/bin/python -m pip install -q -r requirements.txt
.venv/bin/python -m PyInstaller --noconfirm --clean pdfcombine.spec
if [ "$(uname)" = "Darwin" ]; then
  # Strip extended attributes (Finder/iCloud metadata breaks codesign), then ad-hoc sign.
  xattr -cr "dist/PDF Combine.app"
  codesign -s - --force --deep "dist/PDF Combine.app"
  (cd dist && ditto -c -k --keepParent "PDF Combine.app" PDFCombine-macos.zip)
  echo "Built dist/PDF Combine.app and dist/PDFCombine-macos.zip"
fi
