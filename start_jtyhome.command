#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x ".venv/bin/python" ]; then
  echo "[JTYHome] 尚未安装运行环境，先执行：/bin/bash ./install_mac.command"
  exit 2
fi

exec .venv/bin/python app.py --browser default "$@"
