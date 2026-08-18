#!/bin/bash
# One-time, fixed-scope installer for the isolated Ocean Listen runtime.
# v8.1: Homebrew is optional. uv/Python/FFmpeg live inside Ocean's own runtime.
set -euo pipefail
umask 077

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
TASK_DATA_DIR="${JTYHOME_DATA_DIR:-$APP_DIR/.jtyhome-data}"
TASK_RUNTIME_DIR="${OCEAN_RUNTIME_DIR:-$TASK_DATA_DIR/ocean-runtime}"
TASK_VENV_DIR="$TASK_RUNTIME_DIR/.venv"
TASK_BIN_DIR="$TASK_RUNTIME_DIR/bin"
TASK_PYTHON="$TASK_VENV_DIR/bin/python"
TASK_REQUIREMENTS="$APP_DIR/vendor/ocean-listen/requirements.txt"
UV_HOME="$TASK_RUNTIME_DIR/uv-home"
UV_INSTALL_DIR="$TASK_RUNTIME_DIR/uv-bin"
UV_CACHE_DIR="$TASK_RUNTIME_DIR/cache/uv"
UV_PYTHON_INSTALL_DIR="$TASK_RUNTIME_DIR/python"

mkdir -p "$TASK_RUNTIME_DIR" "$TASK_BIN_DIR" "$UV_HOME" "$UV_INSTALL_DIR" "$UV_CACHE_DIR" "$UV_PYTHON_INSTALL_DIR"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "[听海] 一键安装器只支持 macOS。"
  exit 2
fi

export UV_CACHE_DIR UV_PYTHON_INSTALL_DIR

resolve_uv() {
  if command -v uv >/dev/null 2>&1; then
    command -v uv
    return 0
  fi
  if [[ -x "$UV_INSTALL_DIR/uv" ]]; then
    printf '%s\n' "$UV_INSTALL_DIR/uv"
    return 0
  fi
  return 1
}

echo "[听海] 1/7 准备 uv（不要求 Homebrew）"
if ! UV_BIN="$(resolve_uv)"; then
  if ! command -v curl >/dev/null 2>&1; then
    echo "[听海] 没有找到 curl，无法下载独立 uv 安装器。"
    exit 3
  fi
  echo "[听海] 未找到 uv，正在安装到听海自己的运行目录。"
  export UV_INSTALL_DIR
  export UV_NO_MODIFY_PATH=1
  curl -LsSf https://astral.sh/uv/install.sh | sh
  UV_BIN="$(resolve_uv || true)"
  if [[ -z "$UV_BIN" || ! -x "$UV_BIN" ]]; then
    echo "[听海] uv 独立安装失败。"
    exit 3
  fi
fi

echo "[听海] 2/7 准备独立 Python 3.10"
"$UV_BIN" python install 3.10

echo "[听海] 3/7 创建独立环境（不会修改大西瓜主程序）"
"$UV_BIN" venv --python 3.10 --clear "$TASK_VENV_DIR"

echo "[听海] 4/7 安装本机 FFmpeg（不要求系统 FFmpeg / Homebrew）"
"$UV_BIN" pip install --python "$TASK_PYTHON" --upgrade imageio-ffmpeg
FFMPEG_EXE="$("$TASK_PYTHON" - <<'PY'
import imageio_ffmpeg
print(imageio_ffmpeg.get_ffmpeg_exe())
PY
)"
if [[ -z "$FFMPEG_EXE" || ! -x "$FFMPEG_EXE" ]]; then
  echo "[听海] imageio-ffmpeg 没有提供可执行 FFmpeg。"
  exit 4
fi
ln -sf "$FFMPEG_EXE" "$TASK_BIN_DIR/ffmpeg"
"$TASK_BIN_DIR/ffmpeg" -hide_banner -version | head -n 1

echo "[听海] 5/7 安装浅听、Whisper 与音高分析"
"$UV_BIN" pip install --python "$TASK_PYTHON" -r "$TASK_REQUIREMENTS"

echo "[听海] 6/7 安装深听、分轨与人声音色分析"
"$UV_BIN" pip install --python "$TASK_PYTHON" \
  torch torchaudio demucs panns-inference praat-parselmouth

echo "[听海] 7/7 验证独立环境"
PATH="$TASK_BIN_DIR:$TASK_VENV_DIR/bin:${PATH:-/usr/bin:/bin:/usr/sbin:/sbin}" \
IMAGEIO_FFMPEG_EXE="$TASK_BIN_DIR/ffmpeg" \
"$TASK_PYTHON" - <<'PY'
import shutil
import librosa, basic_pitch, faster_whisper, torch, demucs, parselmouth
assert shutil.which("ffmpeg"), "runtime ffmpeg not found on PATH"
print("[听海] Python 组件与本机 FFmpeg 验证通过")
PY

TASK_INSTALLED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf '{"ready":true,"python":"3.10","ffmpeg":"runtime","installer":"uv-standalone","installed_at":"%s","source_commit":"928dfba62a2c074ccb0154f7ddd42743e4ce9e75"}\n' \
  "$TASK_INSTALLED_AT" > "$TASK_RUNTIME_DIR/ready.json"

echo "[听海] 安装完成。无需 Homebrew；回到大西瓜后，等待中的音频会自动开始分析。"
if [[ "${1:-}" != "--service" ]]; then
  echo "按回车关闭这个窗口。"
  read -r _
fi
