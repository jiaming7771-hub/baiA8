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

# ---------- 滑点：入场严、出场宽；紧急逃生可抬到 30% ----------
# 旧 HARD_MIN=500 是 CXMT「一买就亏 5%」的根因：Jupiter 被允许比报价再贵 5%。
# 下限改到 100bps，入场用 ENTRY_MAX_SLIPPAGE_BPS，出场/常规仍可用 MAX_SLIPPAGE_BPS。
_SLIP_BPS_RAW = int(float(os.getenv("PUMP_MAX_SLIPPAGE_BPS", "500")))
SLIPPAGE_BPS_HARD_MIN = 100   # 1% 绝对下限（入场可走更严）
SLIPPAGE_BPS_HARD_MAX = 1000  # 10% 常规绝对天花板
MAX_SLIPPAGE_BPS = max(SLIPPAGE_BPS_HARD_MIN, min(_SLIP_BPS_RAW, SLIPPAGE_BPS_HARD_MAX))
# 入场专用滑点（比出场更严）：默认 250bps=2.5%，堵住「贴顶成交立刻浮亏」
_ENTRY_SLIP_RAW = int(float(os.getenv("PUMP_ENTRY_MAX_SLIPPAGE_BPS", "150")))
ENTRY_MAX_SLIPPAGE_BPS = max(
    SLIPPAGE_BPS_HARD_MIN, min(_ENTRY_SLIP_RAW, MAX_SLIPPAGE_BPS)
)
# 硬止损/时间止损等 urgent 卖出可突破常规硬顶（默认最高 30%）
URGENT_SLIPPAGE_BPS_MAX = max(
    SLIPPAGE_BPS_HARD_MAX,
    min(int(float(os.getenv("PUMP_URGENT_SLIPPAGE_BPS_MAX", "3000"))), 5000),
)

# ---------- 账户级总亏损熔断 ----------
DRAWDOWN_HALT = float(os.getenv("PUMP_DRAWDOWN_HALT", "0.15"))  # 权益相对峰值回撤 ≥ 15%
ABS_LOSS_HALT_SOL = float(os.getenv("PUMP_ABS_LOSS_HALT_SOL", "0.6"))  # 或绝对亏损 ≥ 0.6 SOL

# ---------- RPC / 交易超时与重试 ----------
RPC_TIMEOUT_SEC = float(os.getenv("PUMP_RPC_TIMEOUT_SEC", "20"))
TX_CONFIRM_TIMEOUT_SEC = float(os.getenv("PUMP_TX_CONFIRM_TIMEOUT_SEC", "60"))
RPC_MAX_RETRIES = int(os.getenv("PUMP_RPC_MAX_RETRIES", "3"))

# ---------- 双轨制：主过滤器是「已毕业 + 真深度」，年龄只挡开盘最脏窗口 ----------
# 抽池跑路由 ENTRY_GRADUATED_ONLY 挡（Bubsem 类）；年龄底线默认 45 分钟
TRACK_A_AGE_MIN = float(os.getenv("PUMP_A_AGE_MIN", "45"))
TRACK_A_AGE_MAX = float(os.getenv("PUMP_A_AGE_MAX", "720"))
TRACK_A_REBOUND_MIN = float(os.getenv("PUMP_A_REBOUND_MIN", "0.15"))
TRACK_A_REBOUND_MAX = float(os.getenv("PUMP_A_REBOUND_MAX", "0.80"))
TRACK_A_PULLBACK_MAX = float(os.getenv("PUMP_A_PULLBACK_MAX", "0.20"))
TRACK_A_LIQ_MIN = float(os.getenv("PUMP_A_LIQ_MIN", "25"))
TRACK_A_MIN_TX_M5 = int(float(os.getenv("PUMP_A_MIN_TX_M5", "10")))
TRACK_A_MIN_VOL_M5 = float(os.getenv("PUMP_A_MIN_VOL_M5", "3"))
TRACK_A_BUY_SELL_MIN = float(os.getenv("PUMP_A_BUY_SELL", "1.3"))
# 盈亏不对称修复：止损收紧、TP1 抬高少卖，让赢单能盖住亏单
TRACK_A_HARD_STOP = float(os.getenv("PUMP_A_HARD_STOP", "0.22"))
TRACK_A_TP1 = float(os.getenv("PUMP_A_TP1", "0.40"))
TRACK_A_TP1_SELL = float(os.getenv("PUMP_A_TP1_SELL", "0.35"))
TRACK_A_TRAIL = float(os.getenv("PUMP_A_TRAIL", "0.10"))
TRACK_A_TIME_STOP = float(os.getenv("PUMP_A_TIME_STOP", "12"))

# 轨道 B（更老排行榜盘）；可用 PUMP_TRACK_B=0 关掉
TRACK_B_ENABLED = os.getenv("PUMP_TRACK_B", "1").strip() not in ("0", "false", "False", "")
TRACK_B_AGE_MIN = float(os.getenv("PUMP_B_AGE_MIN", "45"))
TRACK_B_AGE_MAX = float(os.getenv("PUMP_B_AGE_MAX", "1440"))
TRACK_B_LIQ_MIN = float(os.getenv("PUMP_B_LIQ_MIN", "30"))
TRACK_B_PULLBACK_MAX = float(os.getenv("PUMP_B_PULLBACK_MAX", "0.08"))
TRACK_B_MIN_TX_M5 = int(float(os.getenv("PUMP_B_MIN_TX_M5", "15")))
TRACK_B_MIN_VOL_M5 = float(os.getenv("PUMP_B_MIN_VOL_M5", "8"))
TRACK_B_BUY_SELL_MIN = float(os.getenv("PUMP_B_BUY_SELL", "1.5"))
# 放量近似：近 5m 成交额折年化到 1h ≥ h1 成交的该倍数（缺精确前 3h 均量时的替代）
TRACK_B_VOL_SPIKE_RATIO = float(os.getenv("PUMP_B_VOL_SPIKE", "2.5"))
TRACK_B_HARD_STOP = float(os.getenv("PUMP_B_HARD_STOP", "0.22"))
TRACK_B_TP1 = float(os.getenv("PUMP_B_TP1", "0.40"))
TRACK_B_TP1_SELL = float(os.getenv("PUMP_B_TP1_SELL", "0.35"))
TRACK_B_TRAIL = float(os.getenv("PUMP_B_TRAIL", "0.10"))
TRACK_B_TIME_STOP = float(os.getenv("PUMP_B_TIME_STOP", "45"))

# ---------- 进场过滤兼容别名（默认指向轨道 A；旧 env 仍可覆盖）----------
if IS_MOMENTUM:
    _AGE_MIN_DEF, _AGE_MAX_DEF = str(TRACK_A_AGE_MIN), str(TRACK_A_AGE_MAX)
    _LIQ_DEF, _TX_DEF, _VOL_DEF = str(TRACK_A_LIQ_MIN), str(TRACK_A_MIN_TX_M5), str(TRACK_A_MIN_VOL_M5)
    _HARD_DEF, _TP1_DEF, _TP1_SELL_DEF = str(TRACK_A_HARD_STOP), str(TRACK_A_TP1), str(TRACK_A_TP1_SELL)
    _TRAIL_DEF, _TIME_DEF = str(TRACK_A_TRAIL), str(TRACK_A_TIME_STOP)
    _REBOUND_MIN_DEF, _REBOUND_MAX_DEF = str(TRACK_A_REBOUND_MIN), str(TRACK_A_REBOUND_MAX)
else:
    _AGE_MIN_DEF, _AGE_MAX_DEF = "5", "180"
    _LIQ_DEF, _TX_DEF, _VOL_DEF = "5", "5", "1.5"
    _HARD_DEF, _TP1_DEF, _TP1_SELL_DEF = "0.25", "0.18", "0.55"
    _TRAIL_DEF, _TIME_DEF = "0.13", "25"
    _REBOUND_MIN_DEF, _REBOUND_MAX_DEF = "0.25", "0.40"

AGE_MIN_MINUTES = float(os.getenv("PUMP_AGE_MIN", _AGE_MIN_DEF))
AGE_MAX_MINUTES = float(os.getenv("PUMP_AGE_MAX", _AGE_MAX_DEF))

# —— 老盘暴力豁免：超过 AGE_MAX 仍可开，但需极端成交 + 买压（A 轨遗留）——
AGE_EXEMPT_VOLUME_M5_SOL = float(os.getenv("PUMP_AGE_EXEMPT_VOL", "100"))
AGE_EXEMPT_TX_M5 = int(float(os.getenv("PUMP_AGE_EXEMPT_TX", "200")))
AGE_EXEMPT_BUY_SELL_RATIO = float(os.getenv("PUMP_AGE_EXEMPT_BS", "3.0"))

# —— 动量：从 15~30m 低点回升（A 轨默认）——
REBOUND_MIN = float(os.getenv("PUMP_REBOUND_MIN", _REBOUND_MIN_DEF))
REBOUND_MAX = float(os.getenv("PUMP_REBOUND_MAX", _REBOUND_MAX_DEF))
REBOUND_STRICT_FROM = float(os.getenv("PUMP_REBOUND_STRICT_FROM", "0.40"))
REBOUND_STRICT_BUY_SELL = float(os.getenv("PUMP_REBOUND_STRICT_BS", "2.0"))
REBOUND_STRICT_PULLBACK = float(os.getenv("PUMP_REBOUND_STRICT_PB", "0.08"))
BUY_SELL_RATIO_MIN = float(os.getenv("PUMP_BUY_SELL_RATIO", str(TRACK_A_BUY_SELL_MIN)))
PULLBACK_MAX = float(os.getenv("PUMP_PULLBACK_MAX", str(TRACK_A_PULLBACK_MAX)))
# 砸盘残废硬闸：回撤绝对值 > 该阈值直接一票否决（默认 30%）
CRASH_PULLBACK_MAX = max(
    PULLBACK_MAX,
    min(float(os.getenv("PUMP_CRASH_PULLBACK_MAX", "0.30")), 0.95),
)
# 历史最大回撤曾超过该值 → mint 拉黑（默认 50%）
MDD_BLACKLIST_PCT = max(
    0.30, min(float(os.getenv("PUMP_MDD_BLACKLIST_PCT", "0.50")), 0.95)
)
MOMENTUM_STREAK_MIN = int(float(os.getenv("PUMP_MOMENTUM_STREAK", "1")))
# 仿盘 Symbol 黑名单（借用 BTC/SOL 等知名名）；主键永远是 mint
CLONE_SYMBOL_BLOCKLIST = {
    x.strip().upper()
    for x in os.getenv(
        "PUMP_CLONE_SYMBOL_BLOCKLIST",
        "BTC,ETH,SOL,USDT,USDC,WETH,WBTC,BNB,XRP,DOGE,ADA,TRX,TON,"
        "SUI,APT,ARB,OP,LINK,AVAX,NEAR,PEPE,WIF,BONK,POPCAT,TRUMP,"
        "MELANIA,AI16Z,FARTCOIN,JUP,RAY,ORCA,MSOL,JITOSOL",
    ).split(",")
    if x.strip()
}

# —— 旧捡尸参数（仅 dip 模式使用）——
ATH_DROP_MIN = float(os.getenv("PUMP_ATH_DROP", "0.40"))
ATH_DROP_MAX = float(os.getenv("PUMP_ATH_DROP_MAX", "0.80"))
ATH_MAX_MULTIPLIER = float(os.getenv("PUMP_ATH_MAX_MULTIPLIER", "20"))
PANIC_RATIO_MIN = float(os.getenv("PUMP_PANIC_RATIO", "1.2"))
WHALE_DUMP_MIN = float(os.getenv("PUMP_WHALE_DUMP", "0.40"))

LIQUIDITY_MIN_SOL = float(os.getenv("PUMP_LIQ_MIN_SOL", _LIQ_DEF))
MIN_TX_M5 = int(float(os.getenv("PUMP_MIN_TX_M5", _TX_DEF)))
MIN_VOLUME_M5_SOL = float(os.getenv("PUMP_MIN_VOLUME_M5_SOL", _VOL_DEF))

# ---------- 四层出场 + 死盘早砍（默认=A 轨；持仓可覆盖 track）----------
HARD_STOP_PCT = float(os.getenv("PUMP_HARD_STOP_PCT", _HARD_DEF))
TP1_PCT = float(os.getenv("PUMP_TP1_PCT", _TP1_DEF))
TP1_SELL_RATIO = float(os.getenv("PUMP_TP1_SELL", _TP1_SELL_DEF))
TRAIL_DRAWDOWN = float(os.getenv("PUMP_TRAIL_DD", _TRAIL_DEF))
TIME_STOP_MINUTES = float(os.getenv("PUMP_TIME_STOP", _TIME_DEF))
DEAD_CUT_SECONDS = float(os.getenv("PUMP_DEAD_CUT_SEC", "105"))
DEAD_CUT_MIN_PNL = float(os.getenv("PUMP_DEAD_CUT_PNL", "0.03"))
DEAD_CUT_VOL_RATIO = float(os.getenv("PUMP_DEAD_CUT_VOL_RATIO", "0.55"))
# 默认关：活跃度接口常返回 0，易把活盘误判成死盘早砍
DEAD_CUT_ENABLED = os.getenv("PUMP_DEAD_CUT", "0").strip().lower() not in (
    "0",
    "false",
    "False",
    "no",
    "off",
    "",
)

# —— 硬止损二次确认：连续 N 次报价且持续 M 秒仍破线才砍（防插针砍飞）——
# 确认窗口从 6s 收到 3s：今日 hard_stop 平均多磨掉几个点就在确认期
HARD_STOP_CONFIRM_SEC = max(0.0, min(float(os.getenv("PUMP_HARD_STOP_CONFIRM_SEC", "3")), 60.0))
HARD_STOP_CONFIRM_TICKS = max(1, int(float(os.getenv("PUMP_HARD_STOP_CONFIRM_TICKS", "2"))))
# 崩塌线：跌破该值不等确认，立即全仓逃生（始终 ≥ 各轨硬止损）
PANIC_STOP_PCT = max(
    max(TRACK_A_HARD_STOP, TRACK_B_HARD_STOP, HARD_STOP_PCT),
    min(float(os.getenv("PUMP_PANIC_STOP_PCT", "0.30")), 0.95),
)

# —— 早期闷亏早砍（治「一买就红、maxFloat=0」的磨损单）——
# 开仓后一段时间内从未真正浮盈，且当前已明显变红 → 不等硬止损，先砍小亏。
EARLY_FADE_ENABLED = os.getenv("PUMP_EARLY_FADE", "1").strip() not in (
    "0",
    "false",
    "False",
    "",
)
EARLY_FADE_SEC = max(15.0, min(float(os.getenv("PUMP_EARLY_FADE_SEC", "45")), 300.0))
EARLY_FADE_MAX_PEAK = max(
    0.0, min(float(os.getenv("PUMP_EARLY_FADE_MAX_PEAK", "0.03")), 0.30)
)
EARLY_FADE_MIN_LOSS = max(
    0.03, min(float(os.getenv("PUMP_EARLY_FADE_MIN_LOSS", "0.08")), 0.40)
)

# —— 买入前短时确认：信号过线后观察数秒，价格落在窄带内才真下单 ——
ENTRY_CONFIRM_SEC = max(0.0, min(float(os.getenv("PUMP_ENTRY_CONFIRM_SEC", "8")), 30.0))
# 确认窗口内相对起点价的最大允许跌幅，超过即放弃接刀
ENTRY_CONFIRM_MAX_DROP = max(
    0.005, min(float(os.getenv("PUMP_ENTRY_CONFIRM_MAX_DROP", "0.03")), 0.20)
)
# 确认窗口内相对起点价的最大允许涨幅，超过即放弃追高（Nong 类 +9.9% 仍买的主因）
ENTRY_CONFIRM_MAX_RISE = max(
    0.01, min(float(os.getenv("PUMP_ENTRY_CONFIRM_MAX_RISE", "0.05")), 0.50)
)
# 看板价 → 链上价向上偏离上限；超过则放弃（NOTCOON 类 +40% 仍买）
ENTRY_BOARD_CHAIN_DRIFT_MAX = max(
    0.02, min(float(os.getenv("PUMP_ENTRY_BOARD_CHAIN_DRIFT_MAX", "0.08")), 0.50)
)
# Jupiter 报价均价相对确认后链上价的向上偏离上限；超过则取消广播
# 再收到 2%：今日多数「maxFloat=0」磨损单就是确认→成交被吃掉几个点起步
ENTRY_QUOTE_MID_GAP_MAX = max(
    0.01, min(float(os.getenv("PUMP_ENTRY_QUOTE_MID_GAP_MAX", "0.02")), 0.50)
)
# 广播前再读一次链上价：相对确认价再涨超此值 → 取消（报价与广播之间的追价窗口）
ENTRY_PRE_SEND_RISE_MAX = max(
    0.005, min(float(os.getenv("PUMP_ENTRY_PRE_SEND_RISE_MAX", "0.015")), 0.20)
)
# 成交后相对确认价（或真实滑点）超硬顶时，对该 mint 额外冷却（秒）
ENTRY_SLIP_OVERSHOOT_COOLDOWN_SEC = max(
    0.0, float(os.getenv("PUMP_ENTRY_SLIP_OVERSHOOT_COOLDOWN_SEC", "1800"))
)

# —— 进场 5m 涨幅窗口：过冷不进、过热不追 ——
ENTRY_CHG_M5_MIN = float(os.getenv("PUMP_ENTRY_CHG_M5_MIN", "3"))
ENTRY_CHG_M5_MAX = float(os.getenv("PUMP_ENTRY_CHG_M5_MAX", "25"))

# —— 止损后再进：默认走 EXIT_COOLDOWN_SEC；强反转可提前解锁，每 mint 限次 ——
# 默认 0：关掉强反转解锁（实盘同币连环回踩是主亏因）
REENTRY_STRONG_SEC = max(60.0, float(os.getenv("PUMP_REENTRY_STRONG_SEC", "600")))
REENTRY_MAX_RETRY = max(0, int(float(os.getenv("PUMP_REENTRY_MAX_RETRY", "0"))))
# 同 mint 亏损硬封禁（强反转也解不开）：亏 1 次封 N 秒；同日亏 ≥2 次封更久
MINT_LOSS_BAN_1_SEC = max(0.0, float(os.getenv("PUMP_MINT_LOSS_BAN_1_SEC", "7200")))
MINT_LOSS_BAN_2_SEC = max(0.0, float(os.getenv("PUMP_MINT_LOSS_BAN_2_SEC", "86400")))
MINT_LOSS_BAN_FILE = Path(
    os.getenv("PUMP_MINT_LOSS_BAN_FILE", str(DATA_DIR / "mint_loss_bans.json"))
)
# 同名 Symbol 冷却：换 mint 换盘也能绕过 mint 封禁（今天 USWR 开了 4 个不同合约）
# 任一实盘出场后，同 ticker 冷却 N 秒；亏损出场用更长封禁
SYMBOL_COOLDOWN_SEC = max(0.0, float(os.getenv("PUMP_SYMBOL_COOLDOWN_SEC", "21600")))
SYMBOL_LOSS_BAN_SEC = max(0.0, float(os.getenv("PUMP_SYMBOL_LOSS_BAN_SEC", "43200")))
SYMBOL_COOLDOWN_FILE = Path(
    os.getenv("PUMP_SYMBOL_COOLDOWN_FILE", str(DATA_DIR / "symbol_cooldowns.json"))
)

# —— 选币/买币结构优化（数据去伪 / 开发者画像 / 微观结构确认）——
# 数据去伪：拒绝仅凭 m5 代理 m15/m30 的"假连续"入场。
# 开启后，无真实 OHLCV 的候选必须用「自采连涨」作为替代证据（见下）。
# 注意：GeckoTerminal 免费档 OHLCV 常年 429，故不硬依赖 OHLCV，改用本机多周期观察。
ENTRY_REQUIRE_OHLCV = os.getenv("PUMP_ENTRY_REQUIRE_OHLCV", "1").strip() not in (
    "0", "false", "False", "",
)
# 无真实 OHLCV 时，要求本机跨扫描周期自采到的连续上涨次数 ≥ 该值（≈ N×25s 真实观察）。
# 这是我们自己采集、不受限流影响的"真连续"，替代被代理污染的 m15/m30 窗口。
ENTRY_MIN_STREAK_NO_OHLCV = max(
    1, int(float(os.getenv("PUMP_ENTRY_MIN_STREAK_NO_OHLCV", "3")))
)
# 买点微观结构确认：确认窗口内要求价格在起点上方"站住"多次报价，拒绝单针假拉
ENTRY_FLOW_CONFIRM = os.getenv("PUMP_ENTRY_FLOW_CONFIRM", "1").strip() not in (
    "0", "false", "False", "",
)
# 确认窗口内价格 ≥ 起点的最少样本数（防单针）；样本不足时不硬拦（RPC 限流容错）
ENTRY_FLOW_MIN_HOLD_TICKS = max(1, int(float(os.getenv("PUMP_ENTRY_FLOW_MIN_HOLD_TICKS", "2"))))

# —— 开发者/部署者画像否决（治 USWR 类换 mint/换名连环盘）——
CREATOR_BAN_ENABLED = os.getenv("PUMP_CREATOR_BAN", "1").strip() not in (
    "0", "false", "False", "",
)
# creator 名下任一仓位亏损出场后，全 creator 冷却封禁（秒）
CREATOR_LOSS_BAN_SEC = max(0.0, float(os.getenv("PUMP_CREATOR_LOSS_BAN_SEC", "86400")))
# 24h 内同一 creator 在我们的开仓尝试中出现的不同 mint 数 ≥ 该值 → 判定连环发盘，禁买
CREATOR_MAX_DEPLOYS_24H = max(0, int(float(os.getenv("PUMP_CREATOR_MAX_DEPLOYS_24H", "3"))))
CREATOR_STATS_FILE = Path(
    os.getenv("PUMP_CREATOR_STATS_FILE", str(DATA_DIR / "creator_stats.json"))
)

# —— Found 类残盘防御：最低分 + 极早期 bonding curve 禁买 ——
ENTRY_MIN_SCORE = max(0.0, min(float(os.getenv("PUMP_ENTRY_MIN_SCORE", "55")), 100.0))
# 动量模式：相对 ATH/峰值回撤超过该值禁买（残盘/假反弹）
ENTRY_ATH_DROP_MAX = max(
    0.10, min(float(os.getenv("PUMP_ENTRY_ATH_DROP_MAX", "0.35")), 0.90)
)
# pump-fun 曲线进度（real_sol / 毕业阈值）须 ≥ 该百分比；已上 pumpswap 视为 100%
BONDING_MIN_PROGRESS_PCT = max(
    0.0, min(float(os.getenv("PUMP_BONDING_MIN_PROGRESS_PCT", "20")), 100.0)
)
# 只买已毕业（pumpswap）池：bonding curve 盘可被创建者/大户一键抽干
# （Bubsem 类：4 分钟内流动性坍塌 76%，-87% 跑路盘）。毕业池 LP 由协议托管。
ENTRY_GRADUATED_ONLY = os.getenv("PUMP_ENTRY_GRADUATED_ONLY", "1").strip() not in (
    "0", "false", "False", "",
)
# 毕业大约需要 ~85 SOL 真实储备
BONDING_GRADUATION_SOL = max(
    1.0, float(os.getenv("PUMP_BONDING_GRADUATION_SOL", "85"))
)

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
EXIT_SELL_MAX_RETRIES = int(float(os.getenv("PUMP_EXIT_SELL_RETRIES", "4")))
EXIT_SELL_SLIP_STEP_BPS = int(float(os.getenv("PUMP_EXIT_SLIP_STEP_BPS", "200")))
# 非保命单也至少走完整路由梯队+重试（MissingAccount / 毕业迁池常见）
EXIT_SELL_RETRY_NON_URGENT = os.getenv(
    "PUMP_EXIT_SELL_RETRY_NON_URGENT", "1"
).strip() not in ("0", "false", "False", "")
# 全路由流动性坍塌后：强制按 Jupiter 能给的价 salvage（比 write_off=0 强）
EXIT_FORCE_SALVAGE = os.getenv("PUMP_EXIT_FORCE_SALVAGE", "1").strip() not in (
    "0",
    "false",
    "False",
    "",
)
# 卖出兑现校验的成本地板：expect_sol ≥ 成本×该比例，避免盘口已崩时 expect 过低跳过校验
EXIT_EXPECT_COST_FLOOR = max(
    0.20, min(float(os.getenv("PUMP_EXIT_EXPECT_COST_FLOOR", "0.55")), 1.0)
)
# 标记 illiquid 后超过该秒数 → 下一轮强制 urgent salvage
ILLIQUID_FORCE_SELL_SEC = float(os.getenv("PUMP_ILLIQUID_FORCE_SELL_SEC", "25"))
# 持仓期 PumpSwap/曲线 金库 SOL 相对开仓快照骤降 ≥ 该比例 → 立即抽池逃生
# （CXMT：浮盈 +23% 时 SOL 侧被砸干，旧逻辑因 vault=0 读价失败继续沿用假 mark）
VAULT_DRAIN_DROP_PCT = max(
    0.15, min(float(os.getenv("PUMP_VAULT_DRAIN_DROP_PCT", "0.40")), 0.90)
)
# —— 买入广播失败重试：PumpSwap 创作者费 MissingAccount / 过期区块哈希多为瞬时，
#    换路由重新报价后重试；0=不重试 ——
BUY_SEND_MAX_RETRIES = max(0, int(float(os.getenv("PUMP_BUY_SEND_RETRIES", "2"))))

# —— 钱包 SOL 租金 / 底仓保护（防 ATA 创建把余额打干、导致无法止损）——
# Token Account (ATA) rent-exempt 约 0.00203928 SOL；额外留一点余量
# 出场报价兜底：Jupiter 报价能兑现的 SOL 若低于「盘口估值 × (1-该比例)」，
# 说明池子被抽干/盘口价失真（rug），非保命单直接放弃，避免按假价砸出 -90% 的成交。
EXIT_MAX_IMPACT_PCT = max(
    0.05, min(float(os.getenv("PUMP_EXIT_MAX_IMPACT_PCT", "0.40")), 0.95)
)

# 可兑现价值低于该值的仓位视为废币（卖出回款还不够 gas）→ 先强卖一次，仍不行再核销
DUST_WRITEOFF_SOL = float(os.getenv("PUMP_DUST_WRITEOFF_SOL", "0.002"))
# 假涨 TP：可兑现 < 成本×该比例时，禁止 TP1，改为全仓紧急逃生
TP1_REALIZABLE_MIN = max(
    0.40, min(float(os.getenv("PUMP_TP1_REALIZABLE_MIN", "0.75")), 1.0)
)

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
    0.10, min(float(os.getenv("PUMP_HOLDER_TOP10_MAX_PCT", "0.35")), 0.90)
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
# 同 slot 出生聚类（捆绑发射铁证）：前 N 大持仓的 token 账户若 ≥K 个诞生在
# 同一个 slot 且合计仍持有超阈值筹码 → 拦。Bubsem：开盘 slot 9 钱包吃 40.6%。
BUNDLE_SLOT_MIN_WALLETS = max(2, int(os.getenv("PUMP_BUNDLE_SLOT_MIN_WALLETS", "3")))
BUNDLE_SLOT_MAX_PCT = max(0.05, min(float(os.getenv("PUMP_BUNDLE_SLOT_MAX_PCT", "0.15")), 0.90))
# 池子成交等额齐动手（CXMT 验尸）：不看前20持仓榜——农场号大多不在榜上。
# 扫池子最近签名，同 slot / 短窗口内 ≥K 个不同钱包买卖量几乎相等 → 拒买。
FARM_POOL_TX_CHECK_ENABLED = os.getenv("PUMP_FARM_POOL_TX_CHECK", "1").strip() not in (
    "0",
    "false",
    "False",
    "",
)
FARM_POOL_TX_LIMIT = max(20, min(int(os.getenv("PUMP_FARM_POOL_TX_LIMIT", "100")), 200))
FARM_POOL_TX_PARSE = max(10, min(int(os.getenv("PUMP_FARM_POOL_TX_PARSE", "36")), 80))
FARM_POOL_MIN_WALLETS = max(4, min(int(os.getenv("PUMP_FARM_POOL_MIN_WALLETS", "8")), 50))
FARM_POOL_SIZE_TOL = max(0.005, min(float(os.getenv("PUMP_FARM_POOL_SIZE_TOL", "0.02")), 0.10))
# 单笔相对供应量的有效区间（滤灰尘 / 滤真大户单笔）
FARM_POOL_MIN_PCT = max(
    1e-7, min(float(os.getenv("PUMP_FARM_POOL_MIN_PCT", "0.00005")), 0.01)
)
FARM_POOL_MAX_PCT = max(
    FARM_POOL_MIN_PCT,
    min(float(os.getenv("PUMP_FARM_POOL_MAX_PCT", "0.01")), 0.05),
)

# 早期大户净流出熔断：默认关。持仓快照/换手/RPC 做不到 100% 准，误砍（SalaryCat 等）多于救命。
EARLY_WHALE_CHECK_ENABLED = os.getenv(
    "PUMP_EARLY_WHALE_CHECK", "0"
).strip() not in ("0", "false", "False", "")
# 开仓后早期大户净流出监控窗口（秒）与触发阈值
EARLY_WHALE_WINDOW_SEC = float(os.getenv("PUMP_EARLY_WHALE_WINDOW_SEC", "120"))
EARLY_WHALE_DUMP_PCT = max(
    0.05, min(float(os.getenv("PUMP_EARLY_WHALE_DUMP_PCT", "0.20")), 0.90)
)
# 早期监控最短间隔（默认 5s：抢跑砸盘窗口更短，尽量在前几秒发现）
EARLY_WHALE_POLL_SEC = float(os.getenv("PUMP_EARLY_WHALE_POLL_SEC", "5"))
# 成交后静默期：这几秒内不做大户判定，并在期末「重拍基线」。
# 原因：开仓前的快照 + 我们自己的买单 + 成交瞬间的撮合churn，会把正常换手误算成大户流出
# （SalaryCat/BullPad 类：全部在买入后 7~9s 触发，卖完价格就回来了）。
EARLY_WHALE_GRACE_SEC = max(0.0, float(os.getenv("PUMP_EARLY_WHALE_GRACE_SEC", "30")))
# 需要连续 N 次轮询都判定流出才熔断（一次性读数波动不算）
EARLY_WHALE_STRIKES = max(1, int(float(os.getenv("PUMP_EARLY_WHALE_STRIKES", "2"))))
# 熔断后同一 mint 冷却，防连环反复开同一盘（默认 2h，配合亏损硬封禁）
EARLY_WHALE_COOLDOWN_SEC = float(os.getenv("PUMP_EARLY_WHALE_COOLDOWN_SEC", "7200"))
# 价格未明显下跌时不触发大户熔断（防误报砍飞真涨）。
# 默认由 -3% 放宽到 -8%：新币 -3~-4% 只是抖动，配合正常换手会把好仓砍飞。
EARLY_WHALE_MIN_PNL_DROP = float(os.getenv("PUMP_EARLY_WHALE_MIN_PNL_DROP", "-0.08"))
# 止损/斩仓后同一 mint 冷却（秒），防 hard_stop 后立刻再买同一币
EXIT_COOLDOWN_SEC = float(os.getenv("PUMP_EXIT_COOLDOWN_SEC", "1800"))

# ---------- 开仓前「买→卖」往返报价（能买进 ≠ 能卖出）----------
# 用买入所得 token 量立刻反手卖回 SOL，回收率低于该阈值 → 拦截（默认 ≥88%）
ROUNDTRIP_CHECK_ENABLED = os.getenv("PUMP_ROUNDTRIP_CHECK", "1").strip() not in (
    "0",
    "false",
    "False",
    "",
)
ROUNDTRIP_MIN_RECOVERY = max(
    0.50, min(float(os.getenv("PUMP_ROUNDTRIP_MIN_RECOVERY", "0.90")), 0.99)
)
# 往返预检卖出深度倍数：按持仓 N 倍量做反向卖出报价，回收率仍需达标。
# 防「自己能卖出但盘太薄，别人一砸就穿」的假流动性。
ENTRY_ROUNDTRIP_DEPTH_MULT = max(
    1.0, min(float(os.getenv("PUMP_ENTRY_ROUNDTRIP_DEPTH_MULT", "2")), 5.0)
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
