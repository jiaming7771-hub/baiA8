"""真实行情源（实盘专用）。

- 候选发现：DexScreener Boost/Profile 排行榜（主）+ GeckoTerminal new/trending（补）
- 数据刷新：DexScreener tokens/v1 批量（主）+ Gecko pools/multi（补）
- SOL/USD：Binance 现货（直连可达）
- 出境请求统一走 PUMP_HTTP_PROXY（如 Clash http://127.0.0.1:7897）

ATH 口径：各窗口涨跌幅反推的历史高点与观察期 peak 取 max，并限制异常倍数。
whale_dump_pct 是 m15 卖单集中度代理：1 - 卖家数/卖单数。
价差硬过滤已停用：没有真实 bid/ask 时绝不拿 5m 波动冒充盘口价差。
"""

from __future__ import annotations

import json
import logging
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from . import config as C
from .strategy import Candidate

logger = logging.getLogger("pumpfun.market_data")

GECKO_NEW_POOLS = "https://api.geckoterminal.com/api/v2/networks/solana/new_pools?page=1"
GECKO_TRENDING = (
    "https://api.geckoterminal.com/api/v2/networks/solana/"
    "trending_pools?duration=1h&page=1"
)
GECKO_MULTI = "https://api.geckoterminal.com/api/v2/networks/solana/pools/multi/{addrs}"
GECKO_OHLCV = (
    "https://api.geckoterminal.com/api/v2/networks/solana/pools/"
    "{pool}/ohlcv/minute?aggregate=1&limit={limit}&currency=token"
)

# DexScreener 排行榜发现 + tokens 批量行情
DEX_BOOSTS_TOP = "https://api.dexscreener.com/token-boosts/top/v1"
DEX_BOOSTS_LATEST = "https://api.dexscreener.com/token-boosts/latest/v1"
DEX_PROFILES_LATEST = "https://api.dexscreener.com/token-profiles/latest/v1"
DEX_TOKENS_BATCH = "https://api.dexscreener.com/tokens/v1/solana/{addrs}"
# 场所名归一表：同一个场所两家数据源拼写不同（Gecko `meteora-dbc` /
# Dexscreener `meteoradbc`）。不在表里的名字原样保留——ALLOWED_DEXES 是白名单，
# 认不出的自然落在外面。收录 meteora 只是为了拒绝原因/日志里是一个名字。
DEX_ID_MAP = {
    "pumpswap": "pumpswap",
    "pumpfun": "pump-fun",
    "pump": "pump-fun",
    "pump-fun": "pump-fun",
    "meteoradbc": "meteora-dbc",
    "meteora-dbc": "meteora-dbc",
    "meteora_dbc": "meteora-dbc",
}

# data-api.binance.vision 是公开行情镜像（大陆网络通常直连可达）
BINANCE_SOL_URLS = (
    ("https://data-api.binance.vision/api/v3/ticker/price?symbol=SOLUSDT", False),
    ("https://api.binance.com/api/v3/ticker/price?symbol=SOLUSDT", True),  # 代理兜底
)

ALLOWED_DEXES = {"pump-fun", "pumpswap"}


def canon_dex(raw: Any) -> str:
    """归一后的场所名。认不出的原样返回（小写去空白），由白名单去拒。"""
    return DEX_ID_MAP.get(str(raw or "").strip().lower(), str(raw or "").strip().lower())


def is_allowed_dex(raw: Any) -> bool:
    """★ 观察池准入的唯一场所判据，三个 ingest 口都必须过这里。

    漏掉一个口的后果：Gecko 批量兜底刷新曾直接 _update_watch_entry(row)，
    不认场所也不认 SOL_MINT，等于给白名单开了个后门——标不了价的池子照样能
    进观察池，最后变成一个「持有但报不出价」的仓位。
    """
    return canon_dex(raw) in ALLOWED_DEXES

# pump.fun 联合曲线初始参数（恒定乘积）：DexScreener 对未毕业曲线不返回 liquidity，
# 只能由价格反推储备。口径对齐 Gecko 的 reserve = 真实 token × 价格 + 真实 SOL。
CURVE_INIT_VIRTUAL_SOL = 30.0
CURVE_INIT_VIRTUAL_TOKEN = 1_073_000_000.0
CURVE_INIT_REAL_TOKEN = 793_100_000.0
CURVE_K = CURVE_INIT_VIRTUAL_SOL * CURVE_INIT_VIRTUAL_TOKEN
WATCHLIST_FILE = C.DATA_DIR / "live_watchlist.json"
WATCHLIST_MAX = 120

_watchlist: dict[str, dict[str, Any]] = {}
_watchlist_loaded = False
_sol_usd: float = 0.0
_sol_usd_ts: float = 0.0
_last_prices: dict[str, float] = {}  # mint -> price(SOL)
_last_new_scan: float = 0.0
_last_trending_scan: float = 0.0
_last_multi_scan: float = 0.0
_last_dex_discover: float = 0.0
_last_dex_refresh: float = 0.0
# Gecko 429 按通道退避。OHLCV 只是回升信号的精修，发现新池才是命脉：
# 两者共用一个退避会让 OHLCV 的限流把发现通道一起锁死，池子迅速饿死。
_gecko_blocked_until: dict[str, float] = {"discover": 0.0, "ohlcv": 0.0}

# Gecko 免费档很严：新池 ≥45s、批量刷新 ≥90s；429 后退避
NEW_POOLS_MIN_INTERVAL = 45.0
TRENDING_MIN_INTERVAL = 180.0
MULTI_MIN_INTERVAL = 90.0
DEX_DISCOVER_MIN_INTERVAL = 55.0
DEX_REFRESH_MIN_INTERVAL = 40.0
DEX_BATCH_SIZE = 30
# 全量覆盖观察池：漏刷的条目会因数据过旧被当成垃圾隐藏，等于永久出局
DEX_REFRESH_MAX_MINTS = WATCHLIST_MAX
RATE_LIMIT_BACKOFF = 120.0
# 发现通道退避要短（每分钟只打 1~2 次，不是它撑爆配额）；OHLCV 退避要长并让路
GECKO_BACKOFF = {"discover": 45.0, "ohlcv": 240.0}
# 每轮最多几个池拉 OHLCV：这是 Gecko 配额的大头，压住它发现通道才活得下来
OHLCV_MAX_POOLS_PER_SCAN = 3
# Gecko 批量刷新只在 Dex 刷不动、过期占比超过该阈值时才兜底
GECKO_MULTI_STALE_RATIO = 0.35


class MarketDataError(RuntimeError):
    pass


def _proxy_opener() -> urllib.request.OpenerDirector:
    proxy = (C.HTTP_PROXY or "").strip()
    if proxy:
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        return urllib.request.build_opener(handler)
    return urllib.request.build_opener()


def _get_json(
    url: str,
    *,
    use_proxy: bool = True,
    timeout: float | None = None,
    gecko_bucket: str | None = None,
) -> Any:
    """拉 JSON。gecko_bucket 指定 429 退避记到哪条通道（discover / ohlcv）。"""
    timeout = timeout if timeout is not None else C.RPC_TIMEOUT_SEC
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0 (pump-live)"},
    )
    opener = _proxy_opener() if use_proxy else urllib.request.build_opener()
    try:
        with opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            if gecko_bucket:
                backoff = GECKO_BACKOFF.get(gecko_bucket, RATE_LIMIT_BACKOFF)
                _gecko_blocked_until[gecko_bucket] = time.time() + backoff
                raise MarketDataError(f"429 限流，{gecko_bucket} 退避 {backoff:.0f}s") from exc
            raise MarketDataError(f"{url.split('?')[0]} HTTP 429") from exc
        raise MarketDataError(f"{url.split('?')[0]} HTTP {exc.code}") from exc
    except (TimeoutError, urllib.error.URLError, OSError) as exc:
        raise MarketDataError(f"{url.split('?')[0]} 请求失败: {exc}") from exc


def sol_usd_price() -> float:
    """SOL/USD，60 秒缓存（成功或失败都退避，避免每池重试卡顿）。"""
    global _sol_usd, _sol_usd_ts
    now = time.time()
    if now - _sol_usd_ts < 60:
        return _sol_usd
    _sol_usd_ts = now  # 无论成败都推进时间戳 → 60s 退避
    for url, use_proxy in BINANCE_SOL_URLS:
        try:
            data = _get_json(url, use_proxy=use_proxy, timeout=6)
            px = float(data.get("price") or 0)
            if px > 0:
                _sol_usd = px
                return _sol_usd
        except Exception as exc:
            logger.warning("SOL/USD %s 失败: %s", url.split("/")[2], exc)
    logger.warning("SOL/USD 全部源失败，沿用缓存 %.2f", _sol_usd)
    return _sol_usd


def sol_usd_cached() -> float:
    """只读缓存的 SOL/USD，不发网络请求（给事件循环里的看板快照用）。"""
    return _sol_usd


def _load_watchlist() -> None:
    global _watchlist, _watchlist_loaded
    if _watchlist_loaded:
        return
    try:
        if WATCHLIST_FILE.exists():
            _watchlist = json.loads(WATCHLIST_FILE.read_text(encoding="utf-8"))
            logger.info("观察池已恢复 %d 个", len(_watchlist))
    except Exception:
        logger.exception("观察池恢复失败")
        _watchlist = {}
    _watchlist_loaded = True


def _save_watchlist() -> None:
    try:
        WATCHLIST_FILE.write_text(
            json.dumps(_watchlist, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        logger.exception("观察池持久化失败")


def _derive_ath_from_changes(price: float, chg: dict[str, Any]) -> float:
    """由各窗口涨跌幅反推"开盘后真实高点"。

    GeckoTerminal 不直接给 ATH。若 h1 跌幅 -50%，说明 1 小时前价格约为
    price / (1 - 0.5) = 2×price。对 m5/m15/m30/h1/h6/h24 各窗口反推历史价，
    取最大值即为近期高点近似。因目标币寿命 ≤3h，h6/h24 已覆盖其整个生命周期，
    这样即使刚发现该币也能立刻得到真实跌幅（避免 peak 从观察起点算导致跌幅≈0）。
    """
    if price <= 0:
        return 0.0
    best = price
    for key in ("m5", "m15", "m30", "h1", "h6", "h24"):
        try:
            pct = float(chg.get(key) or 0) / 100.0
        except (TypeError, ValueError):
            continue
        # 仅下跌窗口能反推更高历史价；-95% 以下视为异常不采信。
        if pct >= 0 or pct <= -0.95:
            continue
        prev = min(
            price / (1.0 + pct),
            price * max(1.0, float(C.ATH_MAX_MULTIPLIER)),
        )
        if prev > best:
            best = prev
    return best


def _parse_pool(p: dict[str, Any]) -> dict[str, Any] | None:
    try:
        a = p["attributes"]
        rel = p["relationships"]
        dex = rel["dex"]["data"]["id"]
        base_id = rel["base_token"]["data"]["id"]  # solana_<mint>
        mint = base_id.split("_", 1)[1]
        pool_addr = a.get("address") or p.get("id", "").split("_", 1)[-1]
        usd = float(a.get("base_token_price_usd") or 0)
        sol_px = sol_usd_price()
        price_sol = (usd / sol_px) if (usd > 0 and sol_px > 0) else float(a.get("base_token_price_native_currency") or 0)
        created = a.get("pool_created_at")
        listed_ts = (
            datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
            if created
            else time.time()
        )
        tx = a.get("transactions") or {}
        tx5 = tx.get("m5") or {}
        tx15 = tx.get("m15") or {}
        tx1 = tx.get("h1") or {}
        chg = a.get("price_change_percentage") or {}
        vol = a.get("volume_usd") or {}
        name = (a.get("name") or "?").split("/")[0].strip()
        reserve_usd = float(a.get("reserve_in_usd") or 0)
        vol_m5_usd = float(vol.get("m5") or 0)
        vol_h1_usd = float(vol.get("h1") or 0)
        return {
            "mint": mint,
            "pool": pool_addr,
            "dex": canon_dex(dex),
            "symbol": name[:12],
            "listed_at": listed_ts,
            "price_sol": price_sol,
            # 短窗口（恐慌/鲸抛用 m15，活跃度用 m5），h1 作为回落
            "buys_m5": int(tx5.get("buys") or 0),
            "sells_m5": int(tx5.get("sells") or 0),
            "buys_m15": int(tx15.get("buys") or 0),
            "sells_m15": int(tx15.get("sells") or 0),
            "buyers_m15": int(tx15.get("buyers") or 0),
            "sellers_m15": int(tx15.get("sellers") or 0),
            "buys_h1": int(tx1.get("buys") or 0),
            "sells_h1": int(tx1.get("sells") or 0),
            "buyers_h1": int(tx1.get("buyers") or 0),
            "sellers_h1": int(tx1.get("sellers") or 0),
            "chg_m5": float(chg.get("m5") or 0),
            "chg_m15": float(chg.get("m15") or 0),
            "chg_m30": float(chg.get("m30") or 0),
            "chg_m15_real": chg.get("m15") is not None,
            "chg_m30_real": chg.get("m30") is not None,
            "vol_m5_usd": vol_m5_usd,
            "vol_m5_sol": (vol_m5_usd / sol_px) if sol_px > 0 else 0.0,
            "vol_h1_usd": vol_h1_usd,
            "vol_h1_sol": (vol_h1_usd / sol_px) if sol_px > 0 else 0.0,
            "ath_est": _derive_ath_from_changes(price_sol, chg),
            "liquidity_sol": (reserve_usd / sol_px) if sol_px > 0 else 0.0,
        }
    except Exception:
        return None


def _append_px_hist(ent: dict[str, Any], px: float) -> None:
    """追加一条自采价格样本，按时间窗与点数上限裁剪。

    这是唯一不受 Gecko 429 / Dex 缺 m15 影响的价格历史，
    真实回升与插针检测都靠它。
    """
    now = time.time()
    hist = ent.get("px_hist")
    if not isinstance(hist, list):
        hist = []
    # 同轮多路径重复采样 → 就地更新最后一点，不新增（否则点数虚高、老样本被挤掉）
    if hist:
        try:
            last_ts = float(hist[-1][0])
        except (TypeError, ValueError, IndexError):
            last_ts = 0.0
        if now - last_ts < float(C.PX_HIST_MIN_GAP_SEC):
            hist[-1] = [round(now, 1), px]
            ent["px_hist"] = hist
            return
    hist.append([round(now, 1), px])
    cutoff = now - float(C.PX_HIST_WINDOW_MIN) * 60.0
    hist = [
        s
        for s in hist
        if isinstance(s, (list, tuple)) and len(s) == 2 and float(s[0]) >= cutoff
    ]
    cap = int(C.PX_HIST_MAX_POINTS)
    if len(hist) > cap:
        hist = hist[-cap:]
    ent["px_hist"] = hist


def px_hist_stats(ent: dict[str, Any]) -> dict[str, float]:
    """从自采序列导出：窗口低点/高点、覆盖时长、点数、15m 前的价格。"""
    hist = ent.get("px_hist")
    out = {"low": 0.0, "high": 0.0, "span_min": 0.0, "points": 0, "px_15m_ago": 0.0}
    if not isinstance(hist, list) or not hist:
        return out
    pts = []
    for s in hist:
        try:
            ts, px = float(s[0]), float(s[1])
        except (TypeError, ValueError, IndexError):
            continue
        if px > 0:
            pts.append((ts, px))
    if not pts:
        return out
    pts.sort()
    now = time.time()
    out["low"] = min(p[1] for p in pts)
    out["high"] = max(p[1] for p in pts)
    out["span_min"] = max(0.0, (now - pts[0][0]) / 60.0)
    out["points"] = len(pts)
    # 15 分钟前最近的一个样本（够老才算，否则留 0 表示不可用）
    old = [p for p in pts if (now - p[0]) >= 15 * 60.0]
    if old:
        out["px_15m_ago"] = old[-1][1]
    return out


def _update_watch_entry(row: dict[str, Any]) -> None:
    mint = row["mint"]
    ent = _watchlist.get(mint) or {
        "mint": mint,
        "pool": row["pool"],
        "dex": row.get("dex"),
        "symbol": row["symbol"],
        "listed_at": row["listed_at"],
        "peak_price": 0.0,
        "first_seen": time.time(),
    }
    px = float(row.get("price_sol") or 0)
    if px > 0:
        # peak 同时吸收"反推高点"，避免刚发现的币跌幅恒为 0
        ent["peak_price"] = max(
            float(ent.get("peak_price") or 0), px, float(row.get("ath_est") or 0)
        )
        _last_prices[mint] = px
        _append_px_hist(ent, px)
        # 连续上涨 streak：相对上次观察价严格抬升则 +1，否则归零
        prev_px = float(ent.get("price_sol") or 0)
        if prev_px > 0 and px > prev_px * 1.0001:
            ent["price_streak"] = int(ent.get("price_streak") or 0) + 1
        elif prev_px > 0 and px < prev_px * 0.9999:
            ent["price_streak"] = 0
        # 首见或持平：保留原 streak（首见视为 1，便于动量起步）
        elif not ent.get("price_streak"):
            ent["price_streak"] = 1
    ent.update(
        {
            "pool": row.get("pool") or ent.get("pool"),
            "dex": row.get("dex") or ent.get("dex"),
            "price_sol": px,
            "buys_m5": row["buys_m5"],
            "sells_m5": row["sells_m5"],
            "buys_m15": row["buys_m15"],
            "sells_m15": row["sells_m15"],
            "buyers_m15": row["buyers_m15"],
            "sellers_m15": row["sellers_m15"],
            "buys_h1": row["buys_h1"],
            "sells_h1": row["sells_h1"],
            "buyers_h1": row["buyers_h1"],
            "sellers_h1": row["sellers_h1"],
            "chg_m5": row["chg_m5"],
            "chg_m15": row.get("chg_m15", 0),
            "chg_m30": row.get("chg_m30", 0),
            # 数据源是否真给了这两个窗口（否则是 m5/h1 顶替，不可当真实窗口用）
            "chg_m15_real": bool(row.get("chg_m15_real")),
            "chg_m30_real": bool(row.get("chg_m30_real")),
            "vol_m5_usd": row["vol_m5_usd"],
            "vol_m5_sol": row["vol_m5_sol"],
            "vol_h1_sol": row.get("vol_h1_sol", ent.get("vol_h1_sol", 0)),
            "liquidity_sol": row["liquidity_sol"],
            "source": row.get("source") or ent.get("source") or "gecko",
            "updated": time.time(),
        }
    )
    _watchlist[mint] = ent


def _a_age_max_m() -> float:
    return float(C.TRACK_A_AGE_MAX if C.IS_MOMENTUM else C.AGE_MAX_MINUTES)


def _b_age_max_m() -> float:
    if C.IS_MOMENTUM and C.TRACK_B_ENABLED:
        return float(C.TRACK_B_AGE_MAX)
    return float(C.AGE_MAX_MINUTES)


def _evict_stale() -> None:
    """踢出超龄/超量。超容时优先踢买不了的曲线盘和浅池，保留有真深度的毕业盘。"""
    now = time.time()
    age_cap_m = max(_a_age_max_m(), _b_age_max_m()) + 60.0
    max_age_sec = age_cap_m * 60.0
    for mint in list(_watchlist):
        listed = float(_watchlist[mint].get("listed_at") or now)
        if now - listed > max_age_sec:
            _watchlist.pop(mint, None)
    if len(_watchlist) > WATCHLIST_MAX:
        # 排序靠前的先踢：未毕业曲线盘 → 浅池 → 更年轻
        def _kick_key(ent: dict[str, Any]) -> tuple[int, float, float]:
            graduated = "swap" in str(ent.get("dex") or "").lower()
            tier = 0 if (C.ENTRY_GRADUATED_ONLY and not graduated) else 1
            liq = float(ent.get("liquidity_sol") or 0)
            age_s = now - float(ent.get("listed_at") or 0)
            return (tier, liq, age_s)

        ranked = sorted(_watchlist.values(), key=_kick_key)
        overflow = len(_watchlist) - WATCHLIST_MAX
        for ent in ranked[:overflow]:
            _watchlist.pop(ent["mint"], None)


def _ingest_pools(data: dict[str, Any]) -> int:
    added = 0
    for p in data.get("data") or []:
        row = _parse_pool(p)
        if (
            not row
            or not is_allowed_dex(row.get("dex"))
            or row["mint"] == C.SOL_MINT
        ):
            continue
        if float(row.get("liquidity_sol") or 0) < 1.0:
            continue
        row["source"] = "gecko"
        _update_watch_entry(row)
        added += 1
    return added


def _curve_liquidity_sol(price_sol: float) -> float:
    """由价格反推 pump.fun 曲线储备深度（SOL 计价）。非标准曲线会有偏差，
    只作看板筛选用；真实可成交深度由开仓前的 Jupiter 往返审计把关。"""
    if price_sol <= 0:
        return 0.0
    virtual_sol = math.sqrt(CURVE_K * price_sol)
    virtual_token = CURVE_K / virtual_sol
    real_sol = max(0.0, virtual_sol - CURVE_INIT_VIRTUAL_SOL)
    real_token = max(
        0.0,
        virtual_token - (CURVE_INIT_VIRTUAL_TOKEN - CURVE_INIT_REAL_TOKEN),
    )
    return real_token * price_sol + real_sol


def _parse_dex_pair(p: dict[str, Any]) -> dict[str, Any] | None:
    """DexScreener pair → 观察池 row。只收 pump 系 + SOL 计价池。"""
    try:
        if (p.get("chainId") or "").lower() != "solana":
            return None
        dex = canon_dex(p.get("dexId"))
        if not is_allowed_dex(dex):
            return None
        base = p.get("baseToken") or {}
        quote = p.get("quoteToken") or {}
        mint = base.get("address") or ""
        if not mint or mint == C.SOL_MINT:
            return None
        # 只要 SOL/WSOL 计价，避免稳定币对把 priceNative 搞歪
        q_addr = quote.get("address") or ""
        q_sym = (quote.get("symbol") or "").upper()
        if q_addr and q_addr != C.SOL_MINT and q_sym not in ("SOL", "WSOL"):
            return None

        sol_px = sol_usd_price()
        try:
            price_sol = float(p.get("priceNative") or 0)
        except (TypeError, ValueError):
            price_sol = 0.0
        if price_sol <= 0:
            try:
                usd = float(p.get("priceUsd") or 0)
            except (TypeError, ValueError):
                usd = 0.0
            if usd > 0 and sol_px > 0:
                price_sol = usd / sol_px
        if price_sol <= 0:
            return None

        created_ms = p.get("pairCreatedAt")
        listed_ts = (float(created_ms) / 1000.0) if created_ms else time.time()

        tx = p.get("txns") or {}
        tx5 = tx.get("m5") or {}
        tx15 = tx.get("m15") or {}
        tx1 = tx.get("h1") or {}
        # Dex 常无 m15：用 m5×3 近似，避免过滤全挂空
        buys_m5 = int(tx5.get("buys") or 0)
        sells_m5 = int(tx5.get("sells") or 0)
        buys_m15 = int(tx15.get("buys") or 0) or buys_m5 * 3
        sells_m15 = int(tx15.get("sells") or 0) or sells_m5 * 3
        buys_h1 = int(tx1.get("buys") or 0)
        sells_h1 = int(tx1.get("sells") or 0)

        chg = p.get("priceChange") or {}
        chg_m5 = float(chg.get("m5") or 0)
        chg_h1 = float(chg.get("h1") or 0)
        # Dexscreener 从不返回 m15/m30，这里用 m5 / h1 顶替只为让双窗口检查有值；
        # 顶替值绝不可当真实窗口用（回升/插针改吃自采序列），故打上标记。
        m15_real = chg.get("m15") is not None
        m30_real = chg.get("m30") is not None
        chg_m15 = float(chg["m15"]) if m15_real else chg_m5
        chg_m30 = float(chg["m30"]) if m30_real else chg_h1

        vol = p.get("volume") or {}
        vol_m5_usd = float(vol.get("m5") or 0)
        vol_h1_usd = float(vol.get("h1") or 0)
        liq_usd = float((p.get("liquidity") or {}).get("usd") or 0)
        liq_sol = (liq_usd / sol_px) if sol_px > 0 else 0.0
        if liq_sol <= 0 and dex == "pump-fun":
            liq_sol = _curve_liquidity_sol(price_sol)

        chg_dict = {
            "m5": chg_m5,
            "m15": chg_m15,
            "m30": chg_m30,
            "h1": chg_h1,
            "h6": float(chg.get("h6") or 0),
            "h24": float(chg.get("h24") or 0),
        }
        sym = (base.get("symbol") or "?")[:12]
        return {
            "mint": mint,
            "pool": p.get("pairAddress") or "",
            "dex": dex,
            "symbol": sym,
            "listed_at": listed_ts,
            "price_sol": price_sol,
            "buys_m5": buys_m5,
            "sells_m5": sells_m5,
            "buys_m15": buys_m15,
            "sells_m15": sells_m15,
            "buyers_m15": 0,
            "sellers_m15": 0,
            "buys_h1": buys_h1,
            "sells_h1": sells_h1,
            "buyers_h1": 0,
            "sellers_h1": 0,
            "chg_m5": chg_m5,
            "chg_m15": chg_m15,
            "chg_m30": chg_m30,
            "chg_h1": chg_h1,
            "chg_m15_real": m15_real,
            "chg_m30_real": m30_real,
            "vol_m5_usd": vol_m5_usd,
            "vol_m5_sol": (vol_m5_usd / sol_px) if sol_px > 0 else 0.0,
            "vol_h1_usd": vol_h1_usd,
            "vol_h1_sol": (vol_h1_usd / sol_px) if sol_px > 0 else 0.0,
            "ath_est": _derive_ath_from_changes(price_sol, chg_dict),
            "liquidity_sol": liq_sol,
            "source": "dex",
        }
    except Exception:
        return None


def _pick_best_dex_pairs(pairs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同 mint 多池时留流动性最高的 pump 池。"""
    best: dict[str, dict[str, Any]] = {}
    for p in pairs:
        row = _parse_dex_pair(p)
        if not row or float(row.get("liquidity_sol") or 0) < 1.0:
            continue
        mint = row["mint"]
        prev = best.get(mint)
        if not prev or float(row["liquidity_sol"]) > float(prev["liquidity_sol"]):
            best[mint] = row
    return list(best.values())


def _dex_collect_rank_mints() -> list[str]:
    """从 Boost Top / Latest / Profile 拉 Solana mint（去重保序）。"""
    seen: set[str] = set()
    out: list[str] = []
    for url in (DEX_BOOSTS_TOP, DEX_BOOSTS_LATEST, DEX_PROFILES_LATEST):
        try:
            data = _get_json(url, timeout=12)
        except MarketDataError as exc:
            logger.warning("Dex 榜源失败 %s: %s", url.split("/")[3], exc)
            continue
        if not isinstance(data, list):
            continue
        for item in data:
            if not isinstance(item, dict):
                continue
            if (item.get("chainId") or "").lower() != "solana":
                continue
            mint = item.get("tokenAddress") or ""
            if not mint or mint in seen or mint == C.SOL_MINT:
                continue
            seen.add(mint)
            out.append(mint)
    return out


def _dex_fetch_token_rows(mints: list[str]) -> list[dict[str, Any]]:
    """批量 tokens/v1 → 观察池 rows。"""
    rows: list[dict[str, Any]] = []
    if not mints:
        return rows
    for i in range(0, len(mints), DEX_BATCH_SIZE):
        batch = mints[i : i + DEX_BATCH_SIZE]
        url = DEX_TOKENS_BATCH.format(addrs=",".join(batch))
        try:
            data = _get_json(url, timeout=15)
        except MarketDataError as exc:
            logger.warning("Dex tokens 批量失败: %s", exc)
            break
        pairs = data if isinstance(data, list) else []
        rows.extend(_pick_best_dex_pairs(pairs))
        if i + DEX_BATCH_SIZE < len(mints):
            time.sleep(0.35)
    return rows


def _ingest_dex_rows(rows: list[dict[str, Any]]) -> int:
    added = 0
    a_max = _a_age_max_m()
    now = time.time()
    # 先写入 A 龄，再写其余（超容驱逐会优先踢老盘）
    ranked = sorted(
        rows,
        key=lambda r: (
            0 if (now - float(r.get("listed_at") or now)) / 60.0 <= a_max else 1,
            -float(r.get("liquidity_sol") or 0),
        ),
    )
    for row in ranked:
        if not is_allowed_dex(row.get("dex")) or row["mint"] == C.SOL_MINT:
            continue
        _update_watch_entry(row)
        added += 1
    return added


def _dex_discover() -> int:
    mints = _dex_collect_rank_mints()
    if not mints:
        return 0
    rows = _dex_fetch_token_rows(mints)
    n = _ingest_dex_rows(rows)
    logger.info("Dex 排行榜发现 mint=%d pump写入=%d", len(mints), n)
    return n


def _dex_refresh_watchlist() -> int:
    """用 Dex 刷新已在观察池的 mint 行情（不依赖 Gecko）。"""
    mints = [m for m in _watchlist if m]
    if not mints:
        return 0
    # 优先刷较新的，控制批量
    now = time.time()
    mints.sort(
        key=lambda m: float((_watchlist.get(m) or {}).get("listed_at") or 0),
        reverse=True,
    )
    sel = mints[:DEX_REFRESH_MAX_MINTS]
    rows = _dex_fetch_token_rows(sel)
    n = _ingest_dex_rows(rows)
    logger.info("Dex 观察池刷新 写入=%d / 请求=%d", n, len(sel))
    return n


def _stale_ratio() -> float:
    """观察池中行情已过期的占比。过期条目会被当垃圾隐藏，等于没进池。"""
    if not _watchlist:
        return 0.0
    now = time.time()
    max_age = float(C.SIGNAL_MAX_AGE_SEC)
    stale = sum(
        1 for e in _watchlist.values() if now - float(e.get("updated") or 0) > max_age
    )
    return stale / len(_watchlist)


def refresh_watchlist() -> int:
    """Dex 排行榜优先；Gecko 作补源。Gecko 429 不阻断 Dex。"""
    global _last_new_scan, _last_trending_scan, _last_multi_scan
    global _last_dex_discover, _last_dex_refresh
    _load_watchlist()
    now = time.time()

    # 1) Dex 发现（主）
    if now - _last_dex_discover >= DEX_DISCOVER_MIN_INTERVAL:
        try:
            _dex_discover()
            _last_dex_discover = time.time()
        except Exception as exc:
            logger.warning("Dex 发现异常: %s", exc)

    # 2) Dex 刷新观察池
    if time.time() - _last_dex_refresh >= DEX_REFRESH_MIN_INTERVAL:
        try:
            _dex_refresh_watchlist()
            _last_dex_refresh = time.time()
        except Exception as exc:
            logger.warning("Dex 刷新异常: %s", exc)

    # 3) Gecko 补源（冷却期内跳过）
    blocked_until = _gecko_blocked_until["discover"]
    if time.time() < blocked_until:
        logger.info(
            "Gecko 限流冷却中，剩余 %.0fs（Dex 仍可用，观察池 %d）",
            blocked_until - time.time(),
            len(_watchlist),
        )
    else:
        discovered = False
        if now - _last_trending_scan >= TRENDING_MIN_INTERVAL:
            try:
                data = _get_json(GECKO_TRENDING, gecko_bucket="discover")
                _last_trending_scan = time.time()
                discovered = True
                logger.info("Gecko 活跃池补源写入=%d", _ingest_pools(data))
            except MarketDataError as exc:
                logger.warning("Gecko 活跃池失败: %s", exc)
        elif now - _last_new_scan >= NEW_POOLS_MIN_INTERVAL:
            # 毕业迁移会在这里以「新池」出现（pumpswap 池的建池时间=迁移时间），
            # 所以即便只做已毕业盘也必须拉，否则错过刚毕业的池子。
            try:
                data = _get_json(GECKO_NEW_POOLS, gecko_bucket="discover")
                _last_new_scan = time.time()
                discovered = True
                logger.info("Gecko 新池发现写入=%d", _ingest_pools(data))
            except MarketDataError as exc:
                logger.warning("Gecko 新池失败: %s", exc)

        # Dex 刷新正常时这一步纯属浪费配额，只在观察池确实刷不动时才兜底
        if (
            not discovered
            and _stale_ratio() > GECKO_MULTI_STALE_RATIO
            and time.time() - _last_multi_scan >= MULTI_MIN_INTERVAL
            and time.time() >= _gecko_blocked_until["discover"]
        ):
            pools = [e.get("pool") for e in _watchlist.values() if e.get("pool")]
            ok_any = False
            for i in range(0, len(pools), 20):
                if time.time() < _gecko_blocked_until["discover"]:
                    break
                batch = ",".join(pools[i : i + 20])
                try:
                    data = _get_json(
                        GECKO_MULTI.format(addrs=urllib.parse.quote(batch)),
                        gecko_bucket="discover",
                    )
                    ok_any = True
                    for p in data.get("data") or []:
                        row = _parse_pool(p)
                        if not row or row["mint"] == C.SOL_MINT:
                            continue
                        # 这一口以前不过白名单：Gecko 回报的场所名直接落进观察池，
                        # 池子迁移到不支持的场所后还会顶着旧条目继续刷新。
                        if not is_allowed_dex(row.get("dex")):
                            logger.warning(
                                "Gecko 批量刷新丢弃 %s：场所 %s 不在允许清单",
                                row.get("symbol") or row["mint"][:8],
                                canon_dex(row.get("dex")),
                            )
                            continue
                        row["source"] = "gecko"
                        _update_watch_entry(row)
                    time.sleep(1.2)
                except MarketDataError as exc:
                    logger.warning("Gecko 批量刷新失败: %s", exc)
                    break
            if ok_any:
                _last_multi_scan = time.time()

    _evict_stale()
    _save_watchlist()
    return len(_watchlist)


# pool -> (low, high, ok, ts)：命中缓存不打 Gecko，避免 OHLCV 反复撞 429
_ohlcv_cache: dict[str, tuple[float, float, bool, float]] = {}
OHLCV_CACHE_TTL = 90.0


def fetch_pool_ohlcv(
    pool: str, *, lookback_min: int | None = None
) -> tuple[float, float, bool]:
    """拉 Gecko 分钟 K：返回 (low, high, ok)。失败 → (0,0,False)。

    限流冷却期内不发请求；结果缓存 90s。
    """
    if not pool:
        return 0.0, 0.0, False
    now = time.time()
    cached = _ohlcv_cache.get(pool)
    if cached and now - cached[3] < OHLCV_CACHE_TTL:
        return cached[0], cached[1], cached[2]
    if now < max(_gecko_blocked_until["ohlcv"], _gecko_blocked_until["discover"]):
        if cached:
            return cached[0], cached[1], cached[2]
        return 0.0, 0.0, False
    limit = int(lookback_min or C.OHLCV_LOOKBACK_MIN)
    url = GECKO_OHLCV.format(pool=urllib.parse.quote(pool), limit=limit)
    try:
        data = _get_json(url, gecko_bucket="ohlcv")
        rows = (
            ((data or {}).get("data") or {}).get("attributes") or {}
        ).get("ohlcv_list") or []
        lows: list[float] = []
        highs: list[float] = []
        for row in rows:
            # [ts, open, high, low, close, volume]
            if not isinstance(row, (list, tuple)) or len(row) < 5:
                continue
            try:
                hi = float(row[2])
                lo = float(row[3])
            except (TypeError, ValueError):
                continue
            if lo > 0:
                lows.append(lo)
            if hi > 0:
                highs.append(hi)
        if not lows or not highs:
            _ohlcv_cache[pool] = (0.0, 0.0, False, time.time())
            return 0.0, 0.0, False
        out = (min(lows), max(highs), True)
        _ohlcv_cache[pool] = (*out, time.time())
        return out
    except Exception as exc:
        logger.warning("OHLCV 拉取失败 pool=%s…: %s", pool[:8], exc)
        _ohlcv_cache[pool] = (0.0, 0.0, False, time.time())
        return 0.0, 0.0, False


def enrich_ohlcv(cands: list[Candidate], *, only_mints: set[str] | None = None) -> None:
    """就地写入真实 K 线 low/high（仅对指定 mint 或全部；受开关控制）。"""
    if not C.OHLCV_REBOUND_CHECK:
        return
    for c in cands:
        if only_mints is not None and c.mint not in only_mints:
            continue
        if not c.pool:
            continue
        lo, hi, ok = fetch_pool_ohlcv(c.pool)
        c.ohlcv_low = lo
        c.ohlcv_high = hi
        c.ohlcv_ok = ok
        if ok and hi > float(c.ath_price or 0):
            c.ath_price = hi


def build_candidates() -> list[Candidate]:
    """把观察池映射为策略 Candidate（动量字段 + 兼容 dip 的 m15 恐慌/集中度）。"""
    out: list[Candidate] = []
    for ent in _watchlist.values():
        px = float(ent.get("price_sol") or 0)
        peak = float(ent.get("peak_price") or 0)
        if px <= 0 or peak <= 0:
            continue
        sells = int(ent.get("sells_m15") or 0)
        buys = int(ent.get("buys_m15") or 0)
        sellers = int(ent.get("sellers_m15") or 0)
        buys_m5 = int(ent.get("buys_m5") or 0)
        sells_m5 = int(ent.get("sells_m5") or 0)
        chg_m5 = float(ent.get("chg_m5") or 0)
        # 旧观察池可能无 streak：用 5m 正涨幅视为至少 1 次确认，避免永久卡死
        streak = int(ent.get("price_streak") or 0)
        if streak < 1 and chg_m5 > 0:
            streak = 1
        # 卖单集中度代理（0~1）：卖家越少卖单越多 → 越接近 1
        whale = max(0.0, 1.0 - (sellers / sells)) if sells > 0 else 0.0
        hs = px_hist_stats(ent)
        out.append(
            Candidate(
                mint=ent["mint"],
                symbol=ent.get("symbol") or ent["mint"][:6],
                listed_at=float(ent.get("listed_at") or time.time()),
                ath_price=peak,
                price=px,
                buy_vol=float(buys),
                sell_vol=float(sells),
                whale_dump_pct=whale,
                liquidity_sol=float(ent.get("liquidity_sol") or 0),
                tx_count_m5=buys_m5 + sells_m5,
                volume_m5_sol=float(ent.get("vol_m5_sol") or 0),
                volume_m5_usd=float(ent.get("vol_m5_usd") or 0),
                pool=ent.get("pool"),
                dex=ent.get("dex"),
                buys_m5=buys_m5,
                sells_m5=sells_m5,
                chg_m5=chg_m5,
                chg_m15=float(ent.get("chg_m15") or 0),
                chg_m30=float(ent.get("chg_m30") or 0),
                chg_m15_real=bool(ent.get("chg_m15_real")),
                chg_m30_real=bool(ent.get("chg_m30_real")),
                self_low=float(hs["low"]),
                self_high=float(hs["high"]),
                self_span_min=float(hs["span_min"]),
                self_points=int(hs["points"]),
                self_px_15m_ago=float(hs["px_15m_ago"]),
                price_streak=streak,
                data_ts=float(ent.get("updated") or 0),
                volume_h1_sol=float(ent.get("vol_h1_sol") or 0),
                max_drawdown_seen=float(ent.get("max_drawdown_seen") or 0),
            )
        )
    return out


def scan_live() -> list[Candidate]:
    """实盘扫描入口：刷新观察池并产出候选。任何失败都返回空（禁止误买）。"""
    try:
        n = refresh_watchlist()
        cands = build_candidates()
        logger.info("实盘扫描 观察池=%d 候选=%d sol_usd=%.2f", n, len(cands), sol_usd_price())
        return cands
    except Exception:
        logger.exception("实盘扫描异常，返回空")
        return []


def latest_price_map() -> dict[str, float]:
    """观察池最新价（SOL 计价），仅作扫描侧兜底，持仓管仓请用 onchain_price。"""
    return dict(_last_prices)


def lookup_pool(mint: str) -> tuple[str | None, str | None]:
    """返回 (pool, dex)。"""
    ent = _watchlist.get(mint) or {}
    return ent.get("pool"), ent.get("dex")


def lookup_activity(mint: str) -> dict[str, float]:
    """持仓死盘检测用：返回观察池最新 5m 活跃度。"""
    ent = _watchlist.get(mint) or {}
    buys = int(ent.get("buys_m5") or 0)
    sells = int(ent.get("sells_m5") or 0)
    return {
        "volume_m5_sol": float(ent.get("vol_m5_sol") or 0),
        "tx_count_m5": float(buys + sells),
    }
