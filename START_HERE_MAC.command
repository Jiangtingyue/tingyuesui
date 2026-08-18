#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

LOG="$PWD/jtyhome-startup.log"
exec > >(tee -a "$LOG") 2>&1

echo ""
echo "[JTYHome] $(date '+%Y-%m-%d %H:%M:%S')"
echo "[JTYHome] 项目目录：$PWD"

if [ -x ".venv/bin/python" ]; then
  VENV_VERSION="$(.venv/bin/python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || true)"
  case "$VENV_VERSION" in
    3.11|3.12)
      echo "[JTYHome] 已存在兼容环境 Python $VENV_VERSION，直接启动。"
      exec .venv/bin/python app.py --browser default
      ;;
    *)
      echo "[JTYHome] 现有 .venv 版本不兼容，将交给安装器重建。"
      ;;
  esac
fi

exec /bin/bash ./install_mac.command
