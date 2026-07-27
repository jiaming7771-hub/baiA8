#!/usr/bin/env bash
# Pump 实盘看门狗：进程退出后自动拉起（指数退避，上限 60s）。
#
# 注意：macOS TCC 禁止 LaunchAgent 读 Desktop/Documents。
# A8_ROOT 必须落在 ~/Library/Application Support/a8-pump/runtime
#（由 install_live_launchagent.sh / sync_live_runtime.sh 同步）。
set -u

if [[ -n "${A8_ROOT:-}" ]]; then
  ROOT="$A8_ROOT"
else
  ROOT="$(cd "$(dirname "$0")/.." && pwd)"
fi

LOG="${A8_BOT_LOG:-/tmp/a8_bot.log}"
PIDFILE="${A8_BOT_PIDFILE:-/tmp/a8_live_bot.pid}"
STOP_WD="${A8_STOP_WATCHDOG:-/tmp/a8_STOP_WATCHDOG}"
PORT="${A8_BOT_PORT:-8000}"
PYTHON="${ROOT}/backend/.venv/bin/python"

log() {
  echo "$(date '+%F %T') $*" >>"$LOG" 2>/dev/null || echo "$(date '+%F %T') $*" >&2
}

if [[ ! -d "$ROOT" ]]; then
  echo "❌ A8_ROOT 不存在: $ROOT" >&2
  exit 1
fi
if ! cd "$ROOT" 2>/dev/null; then
  echo "❌ 无法进入 A8_ROOT（多为 TCC 拦截 Desktop）: $ROOT" >&2
  echo "   请运行: bash scripts/install_live_launchagent.sh" >&2
  exit 1
fi
if [[ ! -x "$PYTHON" ]]; then
  echo "❌ 找不到 venv python: $PYTHON" >&2
  exit 1
fi
if [[ ! -f "$ROOT/run_live_pump.py" ]]; then
  echo "❌ 找不到 run_live_pump.py: $ROOT" >&2
  exit 1
fi

backoff=2

free_port() {
  local pids
  pids="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)"
  if [[ -n "${pids}" ]]; then
    log "watchdog: free port $PORT -> kill $pids"
    # shellcheck disable=SC2086
    kill $pids 2>/dev/null || true
    sleep 1
    pids="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)"
    if [[ -n "${pids}" ]]; then
      # shellcheck disable=SC2086
      kill -9 $pids 2>/dev/null || true
      sleep 1
    fi
  fi
  # 残留的无端口僵尸也清掉，避免「以为在跑其实已挂」
  pkill -f "$ROOT/run_live_pump.py" 2>/dev/null || true
}

log "===== watchdog start root=$ROOT ====="
rm -f "$STOP_WD"

while true; do
  if [[ -f "$STOP_WD" ]]; then
    log "watchdog: STOP_WATCHDOG 存在，退出保活"
    exit 0
  fi

  free_port
  log "watchdog: starting run_live_pump.py (next_backoff=${backoff}s)"

  "$PYTHON" -u "$ROOT/run_live_pump.py" >>"$LOG" 2>&1 &
  child=$!
  echo "$child" >"$PIDFILE"
  wait "$child"
  ec=$?
  rm -f "$PIDFILE"
  log "watchdog: bot exited code=$ec"

  if (( ec == 0 )); then
    backoff=2
  fi

  if [[ -f "$STOP_WD" ]]; then
    log "watchdog: STOP_WATCHDOG，不再拉起"
    exit 0
  fi

  log "watchdog: restart in ${backoff}s"
  sleep "$backoff"
  if (( backoff < 60 )); then
    backoff=$(( backoff * 2 ))
    if (( backoff > 60 )); then backoff=60; fi
  fi
done
