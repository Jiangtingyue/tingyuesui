#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

REMOTE="ssh://git@ssh.github.com:443/Jiangtingyue/tingyuesui.git"
LOG="$PWD/github-push.log"
exec > >(tee -a "$LOG") 2>&1

echo "[JTYHome] 正在把展开后的源码提交到 $REMOTE"

if ! command -v git >/dev/null 2>&1; then
  echo "[JTYHome] 未找到 git。macOS 需要先具备 Git / Xcode Command Line Tools。"
  exit 2
fi

TMP="$(mktemp -d "${TMPDIR:-/tmp}/jtyhome-push.XXXXXX")"
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT

git clone "$REMOTE" "$TMP/repo"

# Copy this prepared source tree into the clone while excluding private/runtime state.
rsync -a \
  --exclude '.git/' \
  --exclude '.venv/' \
  --exclude '.jtyhome-data/' \
  --exclude '.jtyhome.env' \
  --exclude '.env' \
  --exclude '*.sqlite' \
  --exclude '*.sqlite3' \
  --exclude '*.db' \
  --exclude '*.log' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude '.DS_Store' \
  ./ "$TMP/repo/"

# The source is now expanded; the old release ZIP is no longer needed at repo root.
rm -f "$TMP/repo/JTYHome-v8.9.8-DESKTOP-SLIM-CACHEFIX.zip"

cd "$TMP/repo"
git config user.name "Jiangtingyue"
git config user.email "Jiangtingyue@users.noreply.github.com"
git add -A

if git diff --cached --quiet; then
  echo "[JTYHome] GitHub 已经是当前展开版本，没有需要提交的变化。"
  exit 0
fi

git commit -m "chore: unpack JTYHome v8.9.8 source and add Mac deployment guide"
git push origin HEAD:main

echo "[JTYHome] 完成：源码已展开并推送到 main。"
