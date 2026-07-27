#!/usr/bin/env bash
# Pump 实盘看门狗：进程退出后自动拉起（指数退避，上限 60s）。
# A8_ROOT 指向项目根（默认脚本上级的上级；LaunchAgent 会显式传入）。
set -u

if [[ -n "${A8_ROOT:-}" ]]; then
  ROOT="$A8_ROOT"
else
  ROOT="$(cd "$(dirname "$0")/.." && pwd)"
fi
cd "$ROOT" || {
  echo "❌ 无法进入项目目录: $ROOT" >&2
  exit 1
}

PYTHON="${ROOT}/backend/.venv/bin/python"
LOG="${A8_BOT_LOG:-/tmp/a8_bot.log}"
# pid/停表写 /tmp：LaunchAgent 对 Desktop 目录常无写权限
PIDFILE="${A8_BOT_PIDFILE:-/tmp/a8_live_bot.pid}"
STOP_WD="${A8_STOP_WATCHDOG:-/tmp/a8_STOP_WATCHDOG}"
PORT="${A8_BOT_PORT:-8000}"

if [[ ! -x "$PYTHON" ]]; then
  echo "❌ 找不到 venv python: $PYTHON" >&2
  exit 1
fi

backoff=2

free_port() {
  local pids
  pids="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)"
  if [[ -n "${pids}" ]]; then
    echo "$(date '+%F %T') watchdog: free port $PORT -> kill $pids" >>"$LOG"
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
}

echo "$(date '+%F %T') ===== watchdog start root=$ROOT =====" >>"$LOG"
rm -f "$STOP_WD"

while true; do
  if [[ -f "$STOP_WD" ]]; then
    echo "$(date '+%F %T') watchdog: STOP_WATCHDOG 存在，退出保活" >>"$LOG"
    exit 0
  fi

  free_port
  echo "$(date '+%F %T') watchdog: starting run_live_pump.py (next_backoff=${backoff}s)" >>"$LOG"

  "$PYTHON" -u "$ROOT/run_live_pump.py" >>"$LOG" 2>&1 &
  child=$!
  echo "$child" >"$PIDFILE"
  wait "$child"
  ec=$?
  rm -f "$PIDFILE"
  echo "$(date '+%F %T') watchdog: bot exited code=$ec" >>"$LOG"

  if (( ec == 0 )); then
    backoff=2
  fi

  if [[ -f "$STOP_WD" ]]; then
    echo "$(date '+%F %T') watchdog: STOP_WATCHDOG，不再拉起" >>"$LOG"
    exit 0
  fi

  echo "$(date '+%F %T') watchdog: restart in ${backoff}s" >>"$LOG"
  sleep "$backoff"
  if (( backoff < 60 )); then
    backoff=$(( backoff * 2 ))
    if (( backoff > 60 )); then backoff=60; fi
  fi
done
