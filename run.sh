#!/usr/bin/env bash
# AIFlow launcher for macOS / Linux.
#   ./run.sh              start on port 8000
#   PORT=9000 ./run.sh    start on another port
set -e
cd "$(dirname "$0")"

PORT=${PORT:-8000}

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 nahi mila. Install karo: https://www.python.org/downloads/"
  exit 1
fi

# pehli baar chalane pe virtualenv bana lo
if [ ! -d .venv ]; then
  echo "→ Creating virtual environment (one time)..."
  python3 -m venv .venv
  ./.venv/bin/pip install --quiet --upgrade pip
  ./.venv/bin/pip install --quiet -r requirements.txt
  echo "→ Dependencies installed."
fi

# agar env vars set hain to real model use hoga, warna offline mock
if [ -f .env ]; then
  set -a; . ./.env; set +a
  echo "→ Loaded .env"
fi

echo ""
echo "  AIFlow chal raha hai:  http://localhost:$PORT"
echo "  Rokne ke liye Ctrl+C dabao"
echo ""

exec ./.venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port "$PORT"
