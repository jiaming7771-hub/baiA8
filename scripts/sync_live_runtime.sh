#!/usr/bin/env bash
# 把 Desktop 上的项目同步到 LaunchAgent 可访问的 Application Support runtime。
# 必须在「有 Desktop 权限」的终端里跑（Cursor/Terminal），不要指望 LaunchAgent 自己去读 Desktop。
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
SUPPORT="$HOME/Library/Application Support/a8-pump"
DST="$SUPPORT/runtime"

mkdir -p "$DST"

# 代码/配置同步；实盘数据与 venv 留在 runtime 内持续复用
rsync -a --delete \
  --exclude '.git/' \
  --exclude 'backend/.venv/' \
  --exclude 'backend/pumpfun/data/' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude '.cursor/' \
  --exclude 'node_modules/' \
  "$SRC/" "$DST/"

# 首次：把数据与 venv 拷过去；之后保留 runtime 里的实盘状态
if [[ ! -d "$DST/backend/.venv" ]]; then
  if [[ -d "$SRC/backend/.venv" ]]; then
    echo "→ 复制 venv（首次）…"
    rsync -a "$SRC/backend/.venv/" "$DST/backend/.venv/"
  else
    echo "→ 创建 venv…"
    python3 -m venv "$DST/backend/.venv"
    "$DST/backend/.venv/bin/pip" install -q -r "$DST/backend/requirements.txt"
  fi
fi

if [[ ! -d "$DST/backend/pumpfun/data" ]]; then
  echo "→ 复制实盘 data（首次）…"
  mkdir -p "$DST/backend/pumpfun/data"
  if [[ -d "$SRC/backend/pumpfun/data" ]]; then
    rsync -a "$SRC/backend/pumpfun/data/" "$DST/backend/pumpfun/data/"
  fi
fi

# .env 每次覆盖：Desktop 改 knobs 后 sync 即生效
if [[ -f "$SRC/.env" ]]; then
  cp "$SRC/.env" "$DST/.env"
fi

echo "✅ runtime 已同步"
echo "   src: $SRC"
echo "   dst: $DST"
