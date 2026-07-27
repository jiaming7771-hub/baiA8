#!/usr/bin/env bash
# 安装 macOS LaunchAgent。脚本本体放到 Application Support，
# 避免 Desktop 目录被 TCC 拦截导致 Operation not permitted。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.a8.pump-live"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
SUPPORT="$HOME/Library/Application Support/a8-pump"
WATCHDOG_SRC="$ROOT/scripts/run_live_watchdog.sh"
WATCHDOG_DST="$SUPPORT/run_live_watchdog.sh"
LOG_OUT="/tmp/a8_watchdog.out"
LOG_ERR="/tmp/a8_watchdog.err"

chmod +x "$WATCHDOG_SRC"
mkdir -p "$SUPPORT"
cp "$WATCHDOG_SRC" "$WATCHDOG_DST"
chmod +x "$WATCHDOG_DST"

# 卸旧 agent / 散跑进程，避免抢端口
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
pkill -f 'run_live_watchdog.sh' 2>/dev/null || true
pkill -f 'run_live_pump.py' 2>/dev/null || true
sleep 1
PIDS="$(lsof -tiTCP:8000 -sTCP:LISTEN 2>/dev/null || true)"
if [[ -n "${PIDS}" ]]; then
  # shellcheck disable=SC2086
  kill -9 $PIDS 2>/dev/null || true
fi
rm -f /tmp/a8_STOP_WATCHDOG

cat >"$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>${WATCHDOG_DST}</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${SUPPORT}</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>5</integer>
  <key>StandardOutPath</key>
  <string>${LOG_OUT}</string>
  <key>StandardErrorPath</key>
  <string>${LOG_ERR}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
    <key>A8_ROOT</key>
    <string>${ROOT}</string>
    <key>A8_BOT_LOG</key>
    <string>/tmp/a8_bot.log</string>
    <key>A8_BOT_PIDFILE</key>
    <string>/tmp/a8_live_bot.pid</string>
    <key>A8_STOP_WATCHDOG</key>
    <string>/tmp/a8_STOP_WATCHDOG</string>
  </dict>
</dict>
</plist>
EOF

launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl enable "gui/$(id -u)/${LABEL}" 2>/dev/null || true
launchctl kickstart -k "gui/$(id -u)/${LABEL}" 2>/dev/null || true

echo "✅ LaunchAgent 已安装: $PLIST"
echo "   watchdog: $WATCHDOG_DST"
echo "   项目: $ROOT"
echo "   日志: /tmp/a8_bot.log"
echo "   停保活: touch /tmp/a8_STOP_WATCHDOG && launchctl bootout gui/$(id -u) $PLIST"
echo "   （交易熔断仍用 backend/pumpfun/data/STOP.txt，与看门狗无关）"
