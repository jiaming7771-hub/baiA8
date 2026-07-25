"""Pump.fun 超跌清算捡尸 · 集中配置。"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
TRADING_LOGS_DIR = ROOT / "trading_logs"
STOP_FILE = DATA_DIR / "STOP.txt"
STATE_FILE = DATA_DIR / "bot_state.json"
TRADES_FILE = DATA_DIR / "trades.jsonl"
DAILY_TRADES_FILE = DATA_DIR / "daily_trades.jsonl"
ACCOUNT_FILE = DATA_DIR / "account.json"
LOG_FILE = DATA_DIR / "bot.log"
EXEC_LOG_FILE = TRADING_LOGS_DIR / "bot_execution.log"

# ---------- 资金与仓位 ----------
BANKROLL_SOL = float(os.getenv("PUMP_BANKROLL_SOL", "10"))  # 总资金 10 SOL
POSITION_PCT = float(os.getenv("PUMP_POSITION_PCT", "0.004"))  # 单笔 0.4%
MAX_OPEN_POSITIONS = int(os.getenv("PUMP_MAX_POSITIONS", "3"))
MIN_POSITION_SOL = float(os.getenv("PUMP_MIN_POS_SOL", "0.01"))

# ---------- 过滤阈值 ----------
AGE_MIN_MINUTES = float(os.getenv("PUMP_AGE_MIN", "30"))  # 上线 ≥ 30 分钟
AGE_MAX_MINUTES = float(os.getenv("PUMP_AGE_MAX", "240"))  # 上线 ≤ 4 小时
ATH_DROP_MIN = float(os.getenv("PUMP_ATH_DROP", "0.80"))  # 相对 ATH 跌幅 ≥ 80%
PANIC_RATIO_MIN = float(os.getenv("PUMP_PANIC_RATIO", "2.5"))  # 卖/买 ≥ 2.5
WHALE_DUMP_MIN = float(os.getenv("PUMP_WHALE_DUMP", "0.70"))  # 单户清仓 ≥ 70%
SPREAD_MIN = float(os.getenv("PUMP_SPREAD", "0.04"))  # 价差 > 4%

# ---------- 出场规则 ----------
TP1_PCT = float(os.getenv("PUMP_TP1_PCT", "0.28"))  # 第一止盈 +28%
TP1_SELL_RATIO = float(os.getenv("PUMP_TP1_SELL", "0.55"))  # 卖出 55%
TRAIL_DRAWDOWN = float(os.getenv("PUMP_TRAIL_DD", "0.13"))  # 回撤止盈 13%
TIME_STOP_MINUTES = float(os.getenv("PUMP_TIME_STOP", "11"))  # 时间止损 11 分钟

# ---------- 运行 ----------
SCAN_INTERVAL_SEC = float(os.getenv("PUMP_SCAN_INTERVAL", "8"))
DRY_RUN_DEFAULT = os.getenv("PUMP_DRY_RUN", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)
# 无真实链上源时使用模拟扫描（保证面板可演示）
DEMO_SCAN = os.getenv("PUMP_DEMO_SCAN", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)

DATA_DIR.mkdir(parents=True, exist_ok=True)
TRADING_LOGS_DIR.mkdir(parents=True, exist_ok=True)
