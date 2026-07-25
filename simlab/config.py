"""双子星模拟盘全局配置（可通过环境变量覆盖）。"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"
STATE_PATH = DATA_DIR / "paper_state.json"
TRADES_PATH = DATA_DIR / "trades.jsonl"
CYCLE_LOG_PATH = LOG_DIR / "cycle.log"
PNL_HOURLY_PATH = LOG_DIR / "pnl_hourly.log"
EVENTS_PATH = LOG_DIR / "events.jsonl"

# ---------- 选币 ----------
MIN_QUOTE_VOLUME = float(os.getenv("SIM_MIN_VOLUME", "50000000"))
MAX_ABS_FUNDING = float(os.getenv("SIM_MAX_FUNDING", "0.0003"))
TOP_N = int(os.getenv("SIM_TOP_N", "10"))
SCORE_W_VOLUME = 0.3
SCORE_W_RS = 0.4
SCORE_W_FUNDING = 0.3
EXCLUDE_BASES = frozenset(
    {
        "BTC",
        "ETH",
        "USDC",
        "USDT",
        "BUSD",
        "TUSD",
        "FDUSD",
        "DAI",
        "USD1",
        "RLUSD",
        "USDP",
        "USDE",
        "EUR",
        "AEUR",
        "PAXG",
        "WBTC",
        "WBETH",
        "BETH",
    }
)

# ---------- 模拟盘风控 ----------
INITIAL_EQUITY = float(os.getenv("SIM_INITIAL_EQUITY", "10000"))
MAX_OPEN_POSITIONS = int(os.getenv("SIM_MAX_POSITIONS", "5"))
RISK_PER_TRADE = float(os.getenv("SIM_RISK_PER_TRADE", "0.02"))  # 每笔风险占权益 2%
MAX_POSITION_PCT = float(os.getenv("SIM_MAX_POS_PCT", "0.20"))  # 单仓名义不超过权益 20%
FEE_RATE = float(os.getenv("SIM_FEE_RATE", "0.0004"))  # 单边费率（taker 近似）
SLIPPAGE_BPS = float(os.getenv("SIM_SLIPPAGE_BPS", "2"))  # 成交滑点（万分之二）
PENDING_TTL_CYCLES = int(os.getenv("SIM_PENDING_TTL", "4"))  # 挂单最多存活 4×15m
REQUIRE_PRICE_ABOVE_ENTRY = True  # 现价须高于入场价才挂限价买单

# ---------- 循环 ----------
CYCLE_SECONDS = int(os.getenv("SIM_CYCLE_SECONDS", str(15 * 60)))
HOURLY_PNL_SECONDS = int(os.getenv("SIM_HOURLY_SECONDS", str(60 * 60)))
KLINE_LIMIT = 120

# ---------- HTTP ----------
HTTP_TIMEOUT = float(os.getenv("SIM_HTTP_TIMEOUT", "12"))
BINANCE_FUTURES_HOSTS = [
    "https://fapi.binance.com",
    "https://fapi1.binance.com",
    "https://fapi2.binance.com",
]
BINANCE_SPOT_HOSTS = [
    "https://data-api.binance.vision",
    "https://api.binance.com",
]
OKX_HOSTS = ["https://www.okx.com"]

USER_AGENT = "twin-star-simlab/1.0"

DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
