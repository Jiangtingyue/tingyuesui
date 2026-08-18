#!/usr/bin/env bash
set -u
cd "$(dirname "$0")" || exit 1

if [ -x ".venv/bin/python" ]; then
  JTY_PYTHON=".venv/bin/python"
else
  JTY_PYTHON="${PYTHON:-python3}"
fi

exec "$JTY_PYTHON" app.py --browser chrome "$@"
