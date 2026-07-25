"""Pump.fun 策略配置（含实盘硬风控上限）。

默认 STRATEGY_MODE=momentum（顺势接力）。捡尸 dip 模式仅保留兼容，不再作为默认。
"""

from __future__ import annotations

import os
from pathlib import Path

# 尽早加载 .env（私钥等敏感项只走环境变量）
# override=True：项目 .env 覆盖 shell 里残留的旧 PUMP_*，避免换策略后仍吃到旧阈值
try:
    from wallet import load_dotenv_files

    load_dotenv_files(override=True)
except Exception:
    pass

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

# ---------- 策略模式 ----------
# momentum = 顺势接力/动量突破（默认）；dip = 旧「捡尸」超跌抄底（已废弃，仅兼容）
STRATEGY_MODE = os.getenv("PUMP_STRATEGY_MODE", os.getenv("STRATEGY_MODE", "momentum")).strip().lower()
if STRATEGY_MODE not in ("momentum", "dip"):
    STRATEGY_MODE = "momentum"
IS_MOMENTUM = STRATEGY_MODE == "momentum"

# ---------- 资金与仓位（硬顶：1%~2%，单笔 0.02~0.04 SOL）----------
BANKROLL_SOL = float(os.getenv("PUMP_BANKROLL_SOL", "10"))

_POSITION_PCT_RAW = float(os.getenv("PUMP_POSITION_PCT", "0.01"))
# 绝对硬顶：单笔最多动用权益的 2%，默认/下限按 1%
POSITION_PCT_HARD_MIN = 0.01
POSITION_PCT_HARD_MAX = 0.02
POSITION_PCT = max(POSITION_PCT_HARD_MIN, min(_POSITION_PCT_RAW, POSITION_PCT_HARD_MAX))

MAX_OPEN_POSITIONS = int(os.getenv("PUMP_MAX_POSITIONS", "3"))

_MIN_POS_RAW = float(os.getenv("PUMP_MIN_POS_SOL", "0.02"))
_MAX_POS_RAW = float(os.getenv("PUMP_MAX_POS_SOL", "0.04"))
MIN_POSITION_SOL = max(0.02, _MIN_POS_RAW)  # 硬下限 0.02 SOL
MAX_POSITION_SOL = min(0.04, max(MIN_POSITION_SOL, _MAX_POS_RAW))  # 硬上限 0.04 SOL

# ---------- 滑点硬顶：5%~10%（500~1000 bps），绝不超过 10% ----------
_SLIP_BPS_RAW = int(float(os.getenv("PUMP_MAX_SLIPPAGE_BPS", "500")))
SLIPPAGE_BPS_HARD_MIN = 500   # 5%
SLIPPAGE_BPS_HARD_MAX = 1000  # 10% 绝对天花板
MAX_SLIPPAGE_BPS = max(SLIPPAGE_BPS_HARD_MIN, min(_SLIP_BPS_RAW, SLIPPAGE_BPS_HARD_MAX))

# ---------- 账户级总亏损熔断 ----------
DRAWDOWN_HALT = float(os.getenv("PUMP_DRAWDOWN_HALT", "0.15"))  # 权益相对峰值回撤 ≥ 15%
ABS_LOSS_HALT_SOL = float(os.getenv("PUMP_ABS_LOSS_HALT_SOL", "0.6"))  # 或绝对亏损 ≥ 0.6 SOL

# ---------- RPC / 交易超时与重试 ----------
RPC_TIMEOUT_SEC = float(os.getenv("PUMP_RPC_TIMEOUT_SEC", "20"))
TX_CONFIRM_TIMEOUT_SEC = float(os.getenv("PUMP_TX_CONFIRM_TIMEOUT_SEC", "60"))
RPC_MAX_RETRIES = int(os.getenv("PUMP_RPC_MAX_RETRIES", "3"))

# ---------- 进场过滤（momentum 默认取区间中值；dip 兼容旧值）----------
if IS_MOMENTUM:
    # 基础窗 8~120m；回升 20~40%；活盘 ≥15笔/5SOL；回撤红线 ≤15%
    _AGE_MIN_DEF, _AGE_MAX_DEF = "8", "120"
    _LIQ_DEF, _TX_DEF, _VOL_DEF = "10", "15", "5"
    _HARD_DEF, _TP1_DEF, _TP1_SELL_DEF = "0.13", "0.22", "0.50"
    _TRAIL_DEF, _TIME_DEF = "0.09", "12"
    _REBOUND_MIN_DEF, _REBOUND_MAX_DEF = "0.20", "0.40"
else:
    _AGE_MIN_DEF, _AGE_MAX_DEF = "5", "180"
    _LIQ_DEF, _TX_DEF, _VOL_DEF = "5", "5", "1.5"
    _HARD_DEF, _TP1_DEF, _TP1_SELL_DEF = "0.25", "0.18", "0.55"
    _TRAIL_DEF, _TIME_DEF = "0.13", "25"
    _REBOUND_MIN_DEF, _REBOUND_MAX_DEF = "0.25", "0.40"

AGE_MIN_MINUTES = float(os.getenv("PUMP_AGE_MIN", _AGE_MIN_DEF))
AGE_MAX_MINUTES = float(os.getenv("PUMP_AGE_MAX", _AGE_MAX_DEF))

# —— 老盘暴力豁免：超过 AGE_MAX 仍可开，但需极端成交 + 买压 ——
AGE_EXEMPT_VOLUME_M5_SOL = float(os.getenv("PUMP_AGE_EXEMPT_VOL", "100"))
AGE_EXEMPT_TX_M5 = int(float(os.getenv("PUMP_AGE_EXEMPT_TX", "200")))
AGE_EXEMPT_BUY_SELL_RATIO = float(os.getenv("PUMP_AGE_EXEMPT_BS", "3.0"))

# —— 动量：从 15~30m 低点回升幅度（默认 +20%~+40%）——
REBOUND_MIN = float(os.getenv("PUMP_REBOUND_MIN", _REBOUND_MIN_DEF))
REBOUND_MAX = float(os.getenv("PUMP_REBOUND_MAX", _REBOUND_MAX_DEF))
# —— 动量：近 5m 买/卖比（笔数）——
BUY_SELL_RATIO_MIN = float(os.getenv("PUMP_BUY_SELL_RATIO", "1.3"))
# —— 绝对红线：距短期高点最大回撤（默认 ≤15%，插针/残局一律拒）——
PULLBACK_MAX = float(os.getenv("PUMP_PULLBACK_MAX", "0.15"))
# 连续上涨确认：观察池最近 N 次扫描价格需严格递增（配合 chg_m5>0）
MOMENTUM_STREAK_MIN = int(float(os.getenv("PUMP_MOMENTUM_STREAK", "1")))

# —— 旧捡尸参数（仅 dip 模式使用）——
ATH_DROP_MIN = float(os.getenv("PUMP_ATH_DROP", "0.40"))
ATH_DROP_MAX = float(os.getenv("PUMP_ATH_DROP_MAX", "0.80"))
ATH_MAX_MULTIPLIER = float(os.getenv("PUMP_ATH_MAX_MULTIPLIER", "20"))
PANIC_RATIO_MIN = float(os.getenv("PUMP_PANIC_RATIO", "1.2"))
WHALE_DUMP_MIN = float(os.getenv("PUMP_WHALE_DUMP", "0.40"))

LIQUIDITY_MIN_SOL = float(os.getenv("PUMP_LIQ_MIN_SOL", _LIQ_DEF))
MIN_TX_M5 = int(float(os.getenv("PUMP_MIN_TX_M5", _TX_DEF)))
MIN_VOLUME_M5_SOL = float(os.getenv("PUMP_MIN_VOLUME_M5_SOL", _VOL_DEF))

# ---------- 四层出场 ----------
HARD_STOP_PCT = float(os.getenv("PUMP_HARD_STOP_PCT", _HARD_DEF))
TP1_PCT = float(os.getenv("PUMP_TP1_PCT", _TP1_DEF))
TP1_SELL_RATIO = float(os.getenv("PUMP_TP1_SELL", _TP1_SELL_DEF))
TRAIL_DRAWDOWN = float(os.getenv("PUMP_TRAIL_DD", _TRAIL_DEF))
TIME_STOP_MINUTES = float(os.getenv("PUMP_TIME_STOP", _TIME_DEF))

# ---------- 运行 ----------
SCAN_INTERVAL_SEC = float(os.getenv("PUMP_SCAN_INTERVAL", "25"))
# 持仓链上报价/管仓周期（秒级）；与扫描周期解耦，避免错过瀑布
POSITION_MARK_INTERVAL_SEC = float(os.getenv("PUMP_MARK_INTERVAL", "2"))
# 可选：显式 WSS；默认由 SOLANA_RPC_URL 的 https→wss 推导
SOLANA_WSS_URL = os.getenv("SOLANA_WSS_URL", "").strip()

# 实盘二次确认：PUMP_DRY_RUN=0 且 PUMP_LIVE_CONFIRM=1 才允许默认进 LIVE
_DRY_ENV = os.getenv("PUMP_DRY_RUN", "1").strip().lower()
_WANT_LIVE = _DRY_ENV in ("0", "false", "no", "off")
LIVE_CONFIRM = os.getenv("PUMP_LIVE_CONFIRM", "0").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
if _WANT_LIVE and not LIVE_CONFIRM:
    # 缺确认时强制保持纸面，避免误开实盘
    DRY_RUN_DEFAULT = True
else:
    DRY_RUN_DEFAULT = not _WANT_LIVE

# 无真实链上源时使用模拟扫描（实盘应设 PUMP_DEMO_SCAN=0）
DEMO_SCAN = os.getenv("PUMP_DEMO_SCAN", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)

# ---------- 影子交易（Shadow Trading）----------
# 真行情喂价 + 本地虚拟成交，绝不调用 Jupiter 发链上交易
SHADOW_MODE = os.getenv("SHADOW_MODE", "0").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
# 影子单名义仓位（用户约定：虚拟买入 N SOL）
SHADOW_SIZE_SOL = float(os.getenv("SHADOW_SIZE_SOL", "1.0"))
# 影子摩擦：让虚拟成交贴近实盘。pump 币真实滑点较大，默认单边 3%（双边≈6%）。
# 设为 0 可回到"纯规则"零摩擦测试。
SHADOW_SLIPPAGE_BPS = float(os.getenv("SHADOW_SLIPPAGE_BPS", "300"))
SHADOW_TRADES_FILE = TRADING_LOGS_DIR / "shadow_trades.jsonl"
SHADOW_SUMMARY_FILE = TRADING_LOGS_DIR / "shadow_summary.json"

# ---------- 钱包 / RPC（仅环境变量名；私钥与 API Key 绝不进代码）----------
ENV_SOLANA_KEY = "SOLANA_PRIVATE_KEY"
ENV_WALLET_KEY = "WALLET_PRIVATE_KEY"
SOLANA_RPC_URL = os.getenv(
    "SOLANA_RPC_URL",
    "https://api.mainnet-beta.solana.com",
).strip()

# Jupiter 聚合器（公开报价；不嵌入密钥）。lite-api 为免费新版入口
JUPITER_QUOTE_URL = os.getenv(
    "JUPITER_QUOTE_URL",
    "https://lite-api.jup.ag/swap/v1/quote",
).strip()
JUPITER_SWAP_URL = os.getenv(
    "JUPITER_SWAP_URL",
    "https://lite-api.jup.ag/swap/v1/swap",
).strip()

# 出境 HTTP 代理（Jupiter / GeckoTerminal / DexScreener 被墙时必配，如 Clash）
# 仅行情与聚合器走代理；Helius RPC 保持直连
HTTP_PROXY = os.getenv("PUMP_HTTP_PROXY", "").strip()
SOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL = 1_000_000_000

DATA_DIR.mkdir(parents=True, exist_ok=True)
TRADING_LOGS_DIR.mkdir(parents=True, exist_ok=True)
