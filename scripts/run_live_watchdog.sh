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
  # 残留僵尸：有进程但没监听端口
  pkill -f "$ROOT/run_live_pump.py" 2>/dev/null || true
  sleep 1
}

# 只有 API 真通才算起来；避免「进程在、页面挂」
api_healthy() {
  curl -sf -m 2 "http://127.0.0.1:${PORT}/api/pump/status" >/dev/null 2>&1
}

wait_healthy() {
  local i
  for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    if ! kill -0 "$1" 2>/dev/null; then
      return 1
    fi
    if api_healthy; then
      log "watchdog: HEALTH_OK pid=$1 after ${i}s"
      return 0
    fi
    sleep 1
  done
  log "watchdog: HEALTH_FAIL pid=$1 — API 15s 不通，杀掉重来"
  kill "$1" 2>/dev/null || true
  sleep 1
  kill -9 "$1" 2>/dev/null || true
  return 1
}

log "===== watchdog start root=$ROOT ====="
rm -f "$STOP_WD"

while true; do
  if [[ -f "$STOP_WD" ]]; then
    log "watchdog: STOP_WATCHDOG 存在，退出保活"
    exit 0
  fi

  # 已有健康实例则旁路等待，别互相抢端口
  if api_healthy; then
    live_pid="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -1 || true)"
    log "watchdog: API 已健康 pid=${live_pid:-?}，监视中"
    while api_healthy; do
      if [[ -f "$STOP_WD" ]]; then
        log "watchdog: STOP_WATCHDOG，退出"
        exit 0
      fi
      sleep 5
    done
    log "watchdog: API 掉线，准备拉起"
    free_port
    continue
  fi

  free_port
  log "watchdog: starting run_live_pump.py (next_backoff=${backoff}s)"

  "$PYTHON" -u "$ROOT/run_live_pump.py" >>"$LOG" 2>&1 &
  child=$!
  echo "$child" >"$PIDFILE"

  if wait_healthy "$child"; then
    backoff=2
    wait "$child"
    ec=$?
  else
    wait "$child" 2>/dev/null || true
    ec=98
  fi
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
