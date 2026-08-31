#!/usr/bin/env bash
# Builds aiflow-github.zip — exactly the files that belong on GitHub.
# Chalao:  bash make_zip.sh
set -e
cd "$(dirname "$0")"

OUT="$HOME/aiflow-github.zip"
STAGE="$(mktemp -d)/aiflow"
mkdir -p "$STAGE"

# every tracked file type, explicitly — nothing sensitive can slip in
cp *.py *.sh *.bat *.md requirements.txt Dockerfile compose.yml "$STAGE/" 2>/dev/null || true
cp test_canvas.js "$STAGE/" 2>/dev/null || true
cp .env.example .gitignore .dockerignore "$STAGE/" 2>/dev/null || true
cp -r static docs .github "$STAGE/" 2>/dev/null || true

# never ship runtime state
rm -rf "$STAGE/data" "$STAGE/.venv" "$STAGE/__pycache__" "$STAGE/.env"
find "$STAGE" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
find "$STAGE" -name '*.pyc' -delete 2>/dev/null || true

rm -f "$OUT"
(cd "$(dirname "$STAGE")" && zip -qr "$OUT" aiflow)
rm -rf "$(dirname "$STAGE")"

echo "built: $OUT"
echo "  $(unzip -l "$OUT" | tail -1 | awk '{print $2}') files, $(du -h "$OUT" | cut -f1)"

# safety net: fail loudly if anything private got in
if unzip -l "$OUT" | grep -qE "aiflow\.db|/data/|\.venv|/\.env$"; then
  echo "!! ABORT: private files found in the zip"
  unzip -l "$OUT" | grep -E "aiflow\.db|/data/|\.venv|/\.env$"
  exit 1
fi
echo "  clean — no database, no venv, no secrets"
