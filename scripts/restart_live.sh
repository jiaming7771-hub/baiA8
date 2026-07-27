#!/usr/bin/env bash
# 手动重启 Pump 实盘
#
# 重要：请在「终端.app / iTerm」里自己执行。
# Cursor 对话里代跑的 nohup 进程常被会话回收，看起来就像「又挂了」。
#
# 本脚本走 LaunchAgent + Application Support runtime（系统级保活）。
#
# 用法：
#   bash /Users/bai/Desktop/a8/scripts/restart_live.sh
#   bash /Users/bai/Desktop/a8/scripts/restart_live.sh status
#   bash /Users/bai/Desktop/a8/scripts/restart_live.sh stop
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.a8.pump-live"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
PORT=8000
URL="http://127.0.0.1:${PORT}/api/pump/status"
UI="http://127.0.0.1:${PORT}/"
LOG="/tmp/a8_bot.log"

cd "$ROOT"

status_now() {
  echo "→ 当前状态："
  if pgrep -fl 'a8-pump/runtime/run_live_pump|run_live_pump.py' >/dev/null 2>&1; then
    pgrep -fl 'a8-pump/runtime/run_live_pump|run_live_pump.py' || true
  else
    echo "   进程: 无"
  fi
  if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    lsof -nP -iTCP:"$PORT" -sTCP:LISTEN || true
  else
    echo "   端口 ${PORT}: 空闲"
  fi
  if curl -sf -m 3 "$URL" >/tmp/a8_status.json 2>/dev/null; then
    python3 - <<'PY'
import json
d=json.load(open("/tmp/a8_status.json"))
print(f"   API: OK  running={d.get('running')} halted={d.get('halted')} dry_run={d.get('dry_run')} open={d.get('open_count')}")
PY
  else
    echo "   API: 不通"
  fi
  if launchctl print "gui/$(id -u)/${LABEL}" >/dev/null 2>&1; then
    echo "   LaunchAgent: 已加载"
  else
    echo "   LaunchAgent: 未加载"
  fi
}

stop_all() {
  echo "→ 停止 …"
  touch /tmp/a8_STOP_WATCHDOG 2>/dev/null || true
  launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
  pkill -f 'run_live_watchdog.sh' 2>/dev/null || true
  pkill -f 'run_live_pump.py' 2>/dev/null || true
  pkill -f 'uvicorn main:app' 2>/dev/null || true
  sleep 1
  local pids
  pids="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)"
  if [[ -n "${pids}" ]]; then
    # shellcheck disable=SC2086
    kill -9 $pids 2>/dev/null || true
  fi
  rm -f /tmp/a8_live_bot.pid /tmp/a8_STOP_WATCHDOG
  sleep 1
  echo "   已停止"
}

start_agent() {
  echo "→ 同步代码到 runtime 并安装 LaunchAgent …"
  bash "$ROOT/scripts/install_live_launchagent.sh"
  echo "→ 等待 API …"
  local i
  for i in $(seq 1 30); do
    if curl -sf -m 2 "$URL" >/tmp/a8_status.json 2>/dev/null; then
      echo "✅ 服务已起来（${i}s）"
      python3 - <<'PY'
import json
d=json.load(open("/tmp/a8_status.json"))
print(f"   running={d.get('running')} halted={d.get('halted')} dry_run={d.get('dry_run')} open={d.get('open_count')}")
PY
      echo "   面板: $UI"
      echo "   日志: $LOG"
      return 0
    fi
    sleep 1
  done
  echo "❌ 30 秒内 API 仍不通，最近日志："
  tail -n 50 "$LOG" || true
  exit 1
}

cmd="${1:-restart}"
case "$cmd" in
  stop)
    stop_all
    status_now
    ;;
  status)
    status_now
    ;;
  start|restart|*)
    stop_all
    start_agent
    status_now
    ;;
esac
