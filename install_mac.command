#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

say() { printf '%s\n' "$*"; }

pick_python() {
  local candidate version
  for candidate in python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      version="$($candidate -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || true)"
      case "$version" in
        3.11|3.12)
          command -v "$candidate"
          return 0
          ;;
      esac
    fi
  done
  return 1
}

PY="$(pick_python || true)"
if [ -z "$PY" ]; then
  say "[JTYHome] 需要 Python 3.11 或 3.12（项目要求 >=3.11,<3.13）。"
  say "[JTYHome] 当前没有找到兼容版本；Python 3.13/3.14 不会被错误拿来创建环境。"
  say "[JTYHome] 安装 Python 3.12 后重新运行本脚本即可。"
  exit 2
fi

say "[JTYHome] 使用 $PY ($($PY --version 2>&1))"

if [ -x ".venv/bin/python" ]; then
  VENV_VERSION="$(.venv/bin/python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || true)"
  case "$VENV_VERSION" in
    3.11|3.12) ;;
    *)
      say "[JTYHome] 旧 .venv 的 Python 版本不兼容，正在重建。"
      rm -rf .venv
      ;;
  esac
fi

if [ ! -x ".venv/bin/python" ]; then
  "$PY" -m venv .venv
fi

.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

say "[JTYHome] 安装完成。启动地址：http://127.0.0.1:5175/"
exec .venv/bin/python app.py --browser default
