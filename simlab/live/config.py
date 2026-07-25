"""实盘交易安全配置（默认极度保守；密钥仅从环境变量读取）。"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"
LIVE_STATE_PATH = DATA_DIR / "live_state.json"
LIVE_ORDERS_PATH = DATA_DIR / "live_orders.jsonl"
KILL_SWITCH_PATH = DATA_DIR / "KILL_SWITCH"

# ---------- 交易所 ----------
# okx | binance
EXCHANGE_ID = os.getenv("LIVE_EXCHANGE", "okx").strip().lower()
# 默认测试网/沙盒；生产需显式 LIVE_SANDBOX=0
SANDBOX = os.getenv("LIVE_SANDBOX", "1").strip().lower() not in ("0", "false", "no", "off")
# 真下单开关：默认关闭（只 dry-run）；需 LIVE_TRADING=1 且 CLI --live
LIVE_TRADING = os.getenv("LIVE_TRADING", "0").strip().lower() in ("1", "true", "yes", "on")

# ---------- 资金池与单笔风险（硬封顶）----------
# 可交易资金池占权益比例：默认 5%，可用环境变量上调，但硬顶 20%
_POOL = float(os.getenv("LIVE_POOL_FRACTION", "0.05"))
POOL_FRACTION = max(0.01, min(_POOL, 0.20))
# 单笔风险占权益：默认 0.3%，硬顶 1%
_RISK = float(os.getenv("LIVE_RISK_PERCENT", "0.003"))
RISK_PERCENT = max(0.001, min(_RISK, 0.01))
# 单仓名义占权益上限（额外保险）
MAX_NOTIONAL_FRACTION = float(os.getenv("LIVE_MAX_NOTIONAL_FRACTION", "0.08"))
MAX_NOTIONAL_FRACTION = max(0.01, min(MAX_NOTIONAL_FRACTION, 0.15))

# ---------- 仓位与杠杆 ----------
MAX_OPEN_POSITIONS = int(os.getenv("LIVE_MAX_POSITIONS", "3"))
MAX_LEVERAGE = int(os.getenv("LIVE_MAX_LEVERAGE", "2"))  # 永续杠杆硬顶
MIN_NOTIONAL_USDT = float(os.getenv("LIVE_MIN_NOTIONAL", "5"))

# ---------- 策略过滤 ----------
# 默认只做 hard_pass 前三；允许降级需 LIVE_ALLOW_FALLBACK=1
ALLOW_FALLBACK = os.getenv("LIVE_ALLOW_FALLBACK", "0").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
REQUIRE_PRICE_ABOVE_ENTRY = True
PENDING_TTL_CYCLES = int(os.getenv("LIVE_PENDING_TTL", "4"))

# ---------- 循环 ----------
CYCLE_SECONDS = int(os.getenv("LIVE_CYCLE_SECONDS", str(15 * 60)))

# ---------- 密钥环境变量名（禁止硬编码）----------
ENV_OKX_KEY = "OKX_API_KEY"
ENV_OKX_SECRET = "OKX_SECRET"
ENV_OKX_PASSPHRASE = "OKX_PASSPHRASE"
ENV_BINANCE_KEY = "BINANCE_API_KEY"
ENV_BINANCE_SECRET = "BINANCE_SECRET"

DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
