#!/usr/bin/env bash
# Convenience launcher for local (non-Docker) development.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

echo "Serving on http://localhost:8000 (Ctrl+C to stop)"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
