"""真实行情源（实盘专用）。

- 候选发现：GeckoTerminal new_pools + trending_pools（pump-fun / pumpswap 池）
- 数据刷新：GeckoTerminal pools/multi 批量
- SOL/USD：Binance 现货（直连可达）
- 持仓兜底价：DexScreener tokens 接口
- 出境请求统一走 PUMP_HTTP_PROXY（如 Clash http://127.0.0.1:7897）

ATH 口径：各窗口涨跌幅反推的历史高点与观察期 peak 取 max，并限制异常倍数。
whale_dump_pct 是 m15 卖单集中度代理：1 - 卖家数/卖单数。
价差硬过滤已停用：没有真实 bid/ask 时绝不拿 5m 波动冒充盘口价差。
"""

from __future__ import annotations

import json
import logging
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
# data-api.binance.vision 是公开行情镜像（大陆网络通常直连可达）
BINANCE_SOL_URLS = (
    ("https://data-api.binance.vision/api/v3/ticker/price?symbol=SOLUSDT", False),
    ("https://api.binance.com/api/v3/ticker/price?symbol=SOLUSDT", True),  # 代理兜底
)

ALLOWED_DEXES = {"pump-fun", "pumpswap"}
WATCHLIST_FILE = C.DATA_DIR / "live_watchlist.json"
WATCHLIST_MAX = 80

_watchlist: dict[str, dict[str, Any]] = {}
_watchlist_loaded = False
_sol_usd: float = 0.0
_sol_usd_ts: float = 0.0
_last_prices: dict[str, float] = {}  # mint -> price(SOL)
_last_new_scan: float = 0.0
_last_trending_scan: float = 0.0
_last_multi_scan: float = 0.0
_rate_limited_until: float = 0.0

# Gecko 免费档很严：新池 ≥45s、批量刷新 ≥90s；429 后退避
NEW_POOLS_MIN_INTERVAL = 45.0
TRENDING_MIN_INTERVAL = 180.0
MULTI_MIN_INTERVAL = 90.0
RATE_LIMIT_BACKOFF = 120.0


class MarketDataError(RuntimeError):
    pass


def _proxy_opener() -> urllib.request.OpenerDirector:
    proxy = (C.HTTP_PROXY or "").strip()
    if proxy:
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        return urllib.request.build_opener(handler)
    return urllib.request.build_opener()


def _get_json(url: str, *, use_proxy: bool = True, timeout: float | None = None) -> Any:
    global _rate_limited_until
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
            _rate_limited_until = time.time() + RATE_LIMIT_BACKOFF
            raise MarketDataError(f"429 限流，退避 {RATE_LIMIT_BACKOFF:.0f}s") from exc
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
        return {
            "mint": mint,
            "pool": pool_addr,
            "dex": dex,
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
            "vol_m5_usd": vol_m5_usd,
            "vol_m5_sol": (vol_m5_usd / sol_px) if sol_px > 0 else 0.0,
            "ath_est": _derive_ath_from_changes(price_sol, chg),
            "liquidity_sol": (reserve_usd / sol_px) if sol_px > 0 else 0.0,
        }
    except Exception:
        return None


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
            "vol_m5_usd": row["vol_m5_usd"],
            "vol_m5_sol": row["vol_m5_sol"],
            "liquidity_sol": row["liquidity_sol"],
            "updated": time.time(),
        }
    )
    _watchlist[mint] = ent


def _evict_stale() -> None:
    """踢出超龄或超量的观察对象。

    动量模式保留更久（默认 AGE_MAX+6h），以便「老盘暴力二次拉」仍能进候选。
    """
    now = time.time()
    extra_m = 360.0 if C.IS_MOMENTUM else 60.0
    max_age_sec = (C.AGE_MAX_MINUTES + extra_m) * 60
    for mint in list(_watchlist):
        if now - float(_watchlist[mint].get("listed_at") or now) > max_age_sec:
            _watchlist.pop(mint, None)
    if len(_watchlist) > WATCHLIST_MAX:
        by_age = sorted(_watchlist.values(), key=lambda e: e.get("listed_at") or 0)
        for ent in by_age[: len(_watchlist) - WATCHLIST_MAX]:
            _watchlist.pop(ent["mint"], None)


def _ingest_pools(data: dict[str, Any]) -> int:
    added = 0
    for p in data.get("data") or []:
        row = _parse_pool(p)
        if (
            not row
            or row["dex"] not in ALLOWED_DEXES
            or row["mint"] == C.SOL_MINT
        ):
            continue
        _update_watch_entry(row)
        added += 1
    return added


def refresh_watchlist() -> int:
    """节流刷新：新池 + 活跃池补源；429 冷却期内只读本地观察池。"""
    global _last_new_scan, _last_trending_scan, _last_multi_scan
    _load_watchlist()
    now = time.time()
    if now < _rate_limited_until:
        logger.info(
            "行情限流冷却中，剩余 %.0fs（沿用观察池 %d）",
            _rate_limited_until - now,
            len(_watchlist),
        )
        return len(_watchlist)

    discovered = False
    # trending 优先补充 5~180m 活跃池；每轮最多打一个发现接口，降低 429 风险。
    if now - _last_trending_scan >= TRENDING_MIN_INTERVAL:
        try:
            data = _get_json(GECKO_TRENDING)
            _last_trending_scan = time.time()
            discovered = True
            logger.info("活跃池补源写入=%d", _ingest_pools(data))
        except MarketDataError as exc:
            logger.warning("活跃池发现失败: %s", exc)
    elif now - _last_new_scan >= NEW_POOLS_MIN_INTERVAL:
        try:
            data = _get_json(GECKO_NEW_POOLS)
            _last_new_scan = time.time()
            discovered = True
            logger.info("新池发现写入=%d", _ingest_pools(data))
        except MarketDataError as exc:
            logger.warning("新池发现失败: %s", exc)

    # 发现请求和批量刷新不挤在同轮，防止共享代理 IP 触发免费档 429。
    if (
        not discovered
        and time.time() - _last_multi_scan >= MULTI_MIN_INTERVAL
        and time.time() >= _rate_limited_until
    ):
        pools = [e.get("pool") for e in _watchlist.values() if e.get("pool")]
        ok_any = False
        for i in range(0, len(pools), 20):
            if time.time() < _rate_limited_until:
                break
            batch = ",".join(pools[i : i + 20])
            try:
                data = _get_json(GECKO_MULTI.format(addrs=urllib.parse.quote(batch)))
                ok_any = True
                for p in data.get("data") or []:
                    row = _parse_pool(p)
                    if row:
                        _update_watch_entry(row)
                time.sleep(1.2)
            except MarketDataError as exc:
                logger.warning("观察池批量刷新失败: %s", exc)
                break
        if ok_any:
            _last_multi_scan = time.time()

    _evict_stale()
    _save_watchlist()
    return len(_watchlist)


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
                price_streak=streak,
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
