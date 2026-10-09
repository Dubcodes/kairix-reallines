#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -d .venv ]]; then python3 -m venv .venv; fi
.venv/bin/pip install -r requirements.txt
exec .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
