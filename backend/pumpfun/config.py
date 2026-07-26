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
# 未平仓仓位落盘：进程重启后必须恢复，否则会重复买入且旧仓失去止损托管
POSITIONS_FILE = DATA_DIR / "open_positions.json"
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
    # 基础窗 8~120m；回升 20~70%（>40% 加严）；活盘 ≥15笔/5SOL；回撤红线 ≤15%
    _AGE_MIN_DEF, _AGE_MAX_DEF = "8", "120"
    _LIQ_DEF, _TX_DEF, _VOL_DEF = "10", "15", "5"
    _HARD_DEF, _TP1_DEF, _TP1_SELL_DEF = "0.13", "0.22", "0.50"
    _TRAIL_DEF, _TIME_DEF = "0.09", "12"
    _REBOUND_MIN_DEF, _REBOUND_MAX_DEF = "0.20", "0.70"
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

# —— 动量：从 15~30m 低点回升（20%~70%；>40% 需更严买压+贴高点）——
REBOUND_MIN = float(os.getenv("PUMP_REBOUND_MIN", _REBOUND_MIN_DEF))
REBOUND_MAX = float(os.getenv("PUMP_REBOUND_MAX", _REBOUND_MAX_DEF))
# 回升超过该阈值后启用「延伸加速」门槛
REBOUND_STRICT_FROM = float(os.getenv("PUMP_REBOUND_STRICT_FROM", "0.40"))
REBOUND_STRICT_BUY_SELL = float(os.getenv("PUMP_REBOUND_STRICT_BS", "2.0"))
REBOUND_STRICT_PULLBACK = float(os.getenv("PUMP_REBOUND_STRICT_PB", "0.08"))
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

# ---------- 四层出场 + 死盘早砍 ----------
HARD_STOP_PCT = float(os.getenv("PUMP_HARD_STOP_PCT", _HARD_DEF))
TP1_PCT = float(os.getenv("PUMP_TP1_PCT", _TP1_DEF))
TP1_SELL_RATIO = float(os.getenv("PUMP_TP1_SELL", _TP1_SELL_DEF))
TRAIL_DRAWDOWN = float(os.getenv("PUMP_TRAIL_DD", _TRAIL_DEF))
TIME_STOP_MINUTES = float(os.getenv("PUMP_TIME_STOP", _TIME_DEF))
# 开仓后 N 秒内峰值浮盈仍 < 阈值 → 判定僵尸盘提前清仓（默认 105s / +3%）
DEAD_CUT_SECONDS = float(os.getenv("PUMP_DEAD_CUT_SEC", "105"))
DEAD_CUT_MIN_PNL = float(os.getenv("PUMP_DEAD_CUT_PNL", "0.03"))
# 成交骤降：相对开仓时 5m 成交额低于该比例才叠加确认
DEAD_CUT_VOL_RATIO = float(os.getenv("PUMP_DEAD_CUT_VOL_RATIO", "0.55"))

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

# ---------- 小资金实盘（Micro-Live）----------
# 真钱包签名 + Jupiter 真实换币；单笔固定小额（默认 0.05 SOL，硬顶 0.10）
# 生效条件：PUMP_MICRO_LIVE=1 且 PUMP_DRY_RUN=0 且 PUMP_LIVE_CONFIRM=1
MICRO_LIVE = os.getenv("PUMP_MICRO_LIVE", "0").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
_LIVE_SIZE_RAW = float(os.getenv("PUMP_LIVE_SIZE_SOL", "0.05"))
LIVE_SIZE_SOL_HARD_MIN = 0.01
LIVE_SIZE_SOL_HARD_MAX = 0.10  # 压力测试阶段绝对上限 0.1 SOL
LIVE_SIZE_SOL = max(LIVE_SIZE_SOL_HARD_MIN, min(_LIVE_SIZE_RAW, LIVE_SIZE_SOL_HARD_MAX))

# —— 优先费 / Jito：确保止损单在拥堵时也能快速上链 ——
# jito tip > 0 时走 Jito 捆绑小费；否则用 priorityLevel + maxLamports；两者都空回落 "auto"
PRIORITY_LEVEL = os.getenv("PUMP_PRIORITY_LEVEL", "veryHigh").strip()  # medium|high|veryHigh
PRIORITY_FEE_MAX_LAMPORTS = int(float(os.getenv("PUMP_PRIORITY_FEE_MAX_LAMPORTS", "2000000")))  # 0.002 SOL
JITO_TIP_LAMPORTS = int(float(os.getenv("PUMP_JITO_TIP_LAMPORTS", "0")))

# —— 止损卖出失败重试：每次抬滑点，直到硬顶（绝不允许卡在 Mempool 无法止损）——
EXIT_SELL_MAX_RETRIES = int(float(os.getenv("PUMP_EXIT_SELL_RETRIES", "3")))
EXIT_SELL_SLIP_STEP_BPS = int(float(os.getenv("PUMP_EXIT_SLIP_STEP_BPS", "200")))

# —— 钱包 SOL 租金 / 底仓保护（防 ATA 创建把余额打干、导致无法止损）——
# Token Account (ATA) rent-exempt 约 0.00203928 SOL；额外留一点余量
# 出场报价兜底：Jupiter 报价能兑现的 SOL 若低于「盘口估值 × (1-该比例)」，
# 说明池子被抽干/盘口价失真（rug），非保命单直接放弃，避免按假价砸出 -90% 的成交。
EXIT_MAX_IMPACT_PCT = max(
    0.05, min(float(os.getenv("PUMP_EXIT_MAX_IMPACT_PCT", "0.35")), 0.95)
)

# 可兑现价值低于该值的仓位视为废币（卖出回款还不够 gas）→ 计损核销、释放仓位槽
DUST_WRITEOFF_SOL = float(os.getenv("PUMP_DUST_WRITEOFF_SOL", "0.002"))

# ---------- 买入前链上安全审计（防貔貅/增发/撤池）----------
# 强制校验 mint/freeze 权限已放弃 + 池归属安全程序；拿不到数据一律拦截（fail-closed）
SAFETY_CHECK_ENABLED = os.getenv("PUMP_SAFETY_CHECK", "1").strip() not in ("0", "false", "False", "")
# 影子模式是否也执行安全审计（默认关：影子重在模拟，且省 RPC）
SAFETY_ENFORCE_IN_SHADOW = os.getenv("PUMP_SAFETY_IN_SHADOW", "0").strip() in ("1", "true", "True")
# 单币审计结果缓存秒数，避免每轮扫描重复打 RPC
SAFETY_CACHE_TTL_SEC = float(os.getenv("PUMP_SAFETY_CACHE_TTL_SEC", "300"))

# ---------- 筹码集中度 / 老鼠仓防御 ----------
# 前 N 大非流动性持仓合计占供应量超过该阈值 → 一票否决（默认 40%）
HOLDER_TOP_N = max(5, min(int(os.getenv("PUMP_HOLDER_TOP_N", "10")), 20))
HOLDER_TOP10_MAX_PCT = max(
    0.10, min(float(os.getenv("PUMP_HOLDER_TOP10_MAX_PCT", "0.40")), 0.90)
)
# 流通盘（剔除 bonding curve/vault）内前 N 大占比软顶（默认 70%）
HOLDER_CIRC_MAX_PCT = max(
    0.30, min(float(os.getenv("PUMP_HOLDER_CIRC_MAX_PCT", "0.70")), 0.95)
)
HOLDER_CACHE_TTL_SEC = float(os.getenv("PUMP_HOLDER_CACHE_TTL_SEC", "120"))
# 捆绑/多钱包（Sybil）聚类：同一资金源喂出的多个小号合计控盘超阈值 → 拦
BUNDLE_CHECK_ENABLED = os.getenv("PUMP_BUNDLE_CHECK", "1").strip() not in ("0", "false", "False", "")
BUNDLE_MAX_PCT = max(0.10, min(float(os.getenv("PUMP_BUNDLE_MAX_PCT", "0.35")), 0.90))
# 做资金源聚类探测的前 N 大控制人（每个约 2 次 RPC，勿过大）
BUNDLE_PROBE_OWNERS = max(3, min(int(os.getenv("PUMP_BUNDLE_PROBE_OWNERS", "12")), 20))

# 开仓后早期大户净流出监控窗口（秒）与触发阈值
EARLY_WHALE_WINDOW_SEC = float(os.getenv("PUMP_EARLY_WHALE_WINDOW_SEC", "120"))
EARLY_WHALE_DUMP_PCT = max(
    0.05, min(float(os.getenv("PUMP_EARLY_WHALE_DUMP_PCT", "0.20")), 0.90)
)
# 早期监控最短间隔（默认 5s：抢跑砸盘窗口更短，尽量在前几秒发现）
EARLY_WHALE_POLL_SEC = float(os.getenv("PUMP_EARLY_WHALE_POLL_SEC", "5"))

# ---------- 开仓前「买→卖」往返报价（能买进 ≠ 能卖出）----------
# 用买入所得 token 量立刻反手卖回 SOL，回收率低于该阈值 → 拦截（默认 ≥88%）
ROUNDTRIP_CHECK_ENABLED = os.getenv("PUMP_ROUNDTRIP_CHECK", "1").strip() not in (
    "0",
    "false",
    "False",
    "",
)
ROUNDTRIP_MIN_RECOVERY = max(
    0.50, min(float(os.getenv("PUMP_ROUNDTRIP_MIN_RECOVERY", "0.88")), 0.99)
)
# 买入侧 Jupiter priceImpactPct 硬顶（默认 3%）；超过说明盘口吃不下，易被夹/砸穿
ENTRY_MAX_IMPACT_PCT = max(
    0.005, min(float(os.getenv("PUMP_ENTRY_MAX_IMPACT_PCT", "0.03")), 0.25)
)
# 买入优先直连路由（减少中间 hop = 更难被夹）；失败再回退聚合
ENTRY_PREFER_DIRECT_ROUTES = os.getenv("PUMP_ENTRY_DIRECT_ROUTES", "1").strip() not in (
    "0",
    "false",
    "False",
    "",
)

# ---------- Token-2022 / 元数据 / 黑名单 ----------
# 拦截 transfer fee / permanent delegate / transfer hook / non-transferable
TOKEN2022_EXT_CHECK = os.getenv("PUMP_TOKEN2022_EXT_CHECK", "1").strip() not in (
    "0",
    "false",
    "False",
    "",
)
# Metaplex updateAuthority 未放弃 → 可改名/改社媒做诱饵盘（默认拦）
REQUIRE_REVOKED_UPDATE_AUTH = os.getenv("PUMP_REQUIRE_REVOKED_UPDATE", "1").strip() not in (
    "0",
    "false",
    "False",
    "",
)
# 已知恶名钱包：逗号分隔 + 可选文件（每行一个 base58）
BLACKLIST_WALLETS = {
    x.strip()
    for x in os.getenv("PUMP_BLACKLIST_WALLETS", "").split(",")
    if x.strip()
}
BLACKLIST_FILE = Path(
    os.getenv("PUMP_BLACKLIST_FILE", str(DATA_DIR / "blacklist_wallets.txt"))
)

# ---------- 策略信号防伪（插针假反弹）----------
# 优先用 Gecko OHLCV 真实 low/high 重算 rebound/pullback；失败则回退 + 加严启发式
OHLCV_REBOUND_CHECK = os.getenv("PUMP_OHLCV_REBOUND", "1").strip() not in (
    "0",
    "false",
    "False",
    "",
)
OHLCV_LOOKBACK_MIN = max(5, min(int(os.getenv("PUMP_OHLCV_LOOKBACK_MIN", "30")), 60))
# chg_m5 相对 max(m15,m30) 超过该倍数 → 疑似插针后假反弹
WICK_SPIKE_RATIO = max(1.5, min(float(os.getenv("PUMP_WICK_SPIKE_RATIO", "2.5")), 10.0))
# 候选行情数据超过该秒数视为过旧，禁止开仓（代理限流导致误判）
SIGNAL_MAX_AGE_SEC = float(os.getenv("PUMP_SIGNAL_MAX_AGE_SEC", "90"))

ATA_RENT_SOL = float(os.getenv("PUMP_ATA_RENT_SOL", "0.00203928"))
# 买入后钱包必须至少留这么多 SOL（租金 + gas/优先费底仓）
WALLET_RESERVE_SOL = float(os.getenv("PUMP_WALLET_RESERVE_SOL", "0.05"))
# 总余额低于该地板 → 拒绝一切新开仓（默认 0.2 SOL）
WALLET_MIN_SOL_FLOOR = float(os.getenv("PUMP_WALLET_MIN_SOL", "0.2"))

# ---------- 影子交易（Shadow Trading）----------
# 真行情喂价 + 本地虚拟成交，绝不调用 Jupiter 发链上交易
SHADOW_MODE = os.getenv("SHADOW_MODE", "0").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
# 互斥：Micro-Live 优先，强制关掉影子虚拟成交
if MICRO_LIVE and SHADOW_MODE:
    SHADOW_MODE = False
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
