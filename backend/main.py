"""
加密货币实时价格监控后端（100U 战神短线监控面板）
- 现货：连接币安 WebSocket 获取 BTC/ETH 毫秒级行情（aggTrade + 24h ticker）
- 合约：markPrice 取资金费率、forceOrder 取爆仓流、REST 轮询多空比
- 通过 FastAPI WebSocket 向前端广播
- 交易所/合约接口不可达时，自动切换清晰标注「模拟」的演示数据
- 全链路断线自动重连 + 异常处理
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

from pathlib import Path

import numpy as np
import pandas as pd

# 引入仓库根目录，复用 simlab 综合评分引擎
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import websockets
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

try:
    from simlab.scoring.ranker import rank_ambush_rotation
except Exception:  # pragma: no cover
    rank_ambush_rotation = None  # type: ignore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("crypto-monitor")

try:
    from pumpfun import bot as pump_bot
except Exception as _pump_exc:  # pragma: no cover
    pump_bot = None  # type: ignore
    logger.warning("pumpfun module not loaded: %s", _pump_exc)

try:
    from alt_sim import simulator as alt_sim_bot
except Exception as _alt_exc:  # pragma: no cover
    alt_sim_bot = None  # type: ignore
    logger.warning("alt_sim module not loaded: %s", _alt_exc)
# 币安组合流：aggTrade 毫秒级成交价 + ticker 24h 统计
# 优先使用 data-stream.binance.vision（公开行情 CDN，部分网络更稳定）
BINANCE_WS_URLS = [
    (
        "wss://data-stream.binance.vision/stream"
        "?streams=btcusdt@aggTrade/ethusdt@aggTrade/btcusdt@ticker/ethusdt@ticker"
    ),
    (
        "wss://stream.binance.com:9443/stream"
        "?streams=btcusdt@aggTrade/ethusdt@aggTrade/btcusdt@ticker/ethusdt@ticker"
    ),
    (
        "wss://stream.binance.com:443/stream"
        "?streams=btcusdt@aggTrade/ethusdt@aggTrade/btcusdt@ticker/ethusdt@ticker"
    ),
]

SYMBOL_MAP = {
    "BTCUSDT": "BTC",
    "ETHUSDT": "ETH",
}

# 币安合约 WebSocket：markPrice（含资金费率+下次结算时间）+ forceOrder（爆仓/强平）
BINANCE_FUTURES_WS_URLS = [
    (
        "wss://fstream.binance.com/stream"
        "?streams=btcusdt@markPrice@1s/ethusdt@markPrice@1s"
        "/btcusdt@forceOrder/ethusdt@forceOrder"
    ),
    (
        "wss://fstream.binance.com:443/stream"
        "?streams=btcusdt@markPrice@1s/ethusdt@markPrice@1s"
        "/btcusdt@forceOrder/ethusdt@forceOrder"
    ),
]

# 币安合约 REST：多空持仓人数比
BINANCE_RATIO_HOSTS = [
    "https://fapi.binance.com",
    "https://fapi1.binance.com",
    "https://fapi2.binance.com",
]

# 演示模式：合约接口不可达时用「模拟数据」保证面板不空白（清晰标注 simulated）
# 环境变量 DEMO_FUTURES=off 可强制关闭；=on 可强制开启；默认 auto（不可达时自动开）
DEMO_FUTURES = os.getenv("DEMO_FUTURES", "auto").lower()

app = FastAPI(title="Crypto Price Monitor", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


class ConnectionManager:
    """管理前端 WebSocket 连接并广播消息。"""

    def __init__(self) -> None:
        self.active: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self.active.add(websocket)
        logger.info("Frontend connected. clients=%d", len(self.active))

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self.active.discard(websocket)
        logger.info("Frontend disconnected. clients=%d", len(self.active))

    async def broadcast(self, message: dict[str, Any]) -> None:
        if not self.active:
            return
        payload = json.dumps(message, ensure_ascii=False)
        async with self._lock:
            clients = list(self.active)
        dead: list[WebSocket] = []
        for ws in clients:
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self.disconnect(ws)


manager = ConnectionManager()

# 最新行情缓存，供新连接的客户端立即同步
latest_prices: dict[str, dict[str, Any]] = {
    "BTC": {
        "symbol": "BTC",
        "price": None,
        "high_24h": None,
        "low_24h": None,
        "change_24h": None,
        "change_pct_24h": None,
        "volume_24h": None,
        "ts": None,
    },
    "ETH": {
        "symbol": "ETH",
        "price": None,
        "high_24h": None,
        "low_24h": None,
        "change_24h": None,
        "change_pct_24h": None,
        "volume_24h": None,
        "ts": None,
    },
}

exchange_status = {
    "connected": False,
    "last_error": None,
    "reconnect_attempt": 0,
}

# 合约维度缓存：资金费率 / 多空比
futures_data: dict[str, dict[str, Any]] = {
    "BTC": {
        "symbol": "BTC",
        "funding_rate": None,   # 小数，如 0.0001 = 0.01%
        "next_funding_ts": None,  # 下次结算 UTC ISO
        "mark_price": None,
        "long_short_ratio": None,
        "long_account": None,
        "short_account": None,
        "simulated": False,
        "ts": None,
    },
    "ETH": {
        "symbol": "ETH",
        "funding_rate": None,
        "next_funding_ts": None,
        "mark_price": None,
        "long_short_ratio": None,
        "long_account": None,
        "short_account": None,
        "simulated": False,
        "ts": None,
    },
}

futures_status = {
    "ws_connected": False,      # markPrice / forceOrder 流
    "ratio_ok": False,         # 多空比 REST
    "demo_active": False,      # 是否正在输出模拟数据
    "last_error": None,
}

# ---------- 稳健山寨轮动雷达 + 大盘风控 ----------
MIN_QUOTE_VOLUME = 50_000_000   # 24h 成交额 > 5000 万 USDT
MAX_ABS_FUNDING = 0.0003        # |费率| > 0.03% 剔除（防过热）
RADAR_TOP_N = 10                # 双子星综合指标 TOP 10
RADAR_INTERVAL_SEC = 60
RADAR_SHORTLIST = 40            # 流动性初筛后最多评估相对强度的数量
# 大盘暴跌判定：1h 跌幅或「中等下跌 + 高波动」触发全局禁多
BTC_CRASH_CHANGE_1H = -1.5      # %
BTC_SOFT_CRASH_1H = -0.8        # %
BTC_HIGH_VOL_1H = 1.8           # % 近 1h 已实现波动
EXCLUDE_BASES = {
    "BTC", "ETH", "USDC", "USDT", "BUSD", "TUSD", "FDUSD", "DAI", "USD1",
    "RLUSD", "USDP", "USDE", "EUR", "AEUR", "PAXG", "WBTC", "WBETH", "BETH",
}
# 山寨季判定：雷达前列出现这些 L1 / SOL 联动币时，优先给「传导期」建议
ALT_SEASON_BASES = {
    "SOL", "AVAX", "SUI", "APT", "NEAR", "SEI", "TIA", "INJ", "ATOM",
    "OP", "ARB", "TON", "ADA", "DOT", "LINK", "FIL", "ICP",
}

# 双子星白名单缓存（避免每分钟打爆 instruments 接口）
_twin_star_cache: dict[str, Any] = {"ts": 0.0, "bases": set(), "meta": {}}
TWIN_STAR_CACHE_SEC = 600

FUTURES_REST_HOSTS = [
    "https://fapi.binance.com",
    "https://fapi1.binance.com",
    "https://fapi2.binance.com",
]
SPOT_REST_HOSTS = [
    "https://data-api.binance.vision",
    "https://api.binance.com",
]

radar_state: dict[str, Any] = {
    "items": [],
    "top3": [],
    "top3_fallback": False,
    "btc_change_1h": None,
    "source": "init",       # futures | spot_proxy | demo
    "simulated": False,
    "updated_at": None,
    "watch_symbols": set(),  # 前端主动 watch 的山寨（动态订阅）
}

market_safety: dict[str, Any] = {
    "status": "safe",           # safe | risk | season | rotate | watch
    "no_long": False,           # 全局禁多令
    "label": "等待山寨轮动建议…",
    "advice_mode": "watch",     # risk | season | rotate | watch
    "advice_title": "等待山寨轮动建议…",
    "advice_basis": "雷达扫描与大盘风控数据同步中",
    "btc_change_1h": None,
    "btc_volatility_1h": None,
    "btc_trend": "flat",        # up | down | flat
    "reason": "",
    "updated_at": None,
}

radar_watch_event: asyncio.Event | None = None

# ---------- 多交易所聚合（Binance / OKX / Bybit 永续） ----------
AGG_EXCHANGES = ("binance", "okx", "bybit")
AGG_SYMBOLS = ("BTC", "ETH")
AGG_BROADCAST_SEC = 1.0     # 聚合结果广播节流
AGG_REST_POLL_SEC = 3.0     # REST 降级轮询间隔
AGG_ARB_ALERT_BPS = 8.0     # 价差超过该基点视为套利机会提示
AGG_STALE_SEC = 20          # 超过该秒数未更新视为失效

# 演示模式：交易所完全不可达时用「模拟」报价（清晰标注 simulated）
DEMO_EXCHANGES = os.getenv("DEMO_EXCHANGES", "auto").lower()

OKX_REST_HOSTS = ["https://www.okx.com"]
OKX_WS_URLS = [
    "wss://ws.okx.com:8443/ws/v5/public",
    "wss://wsaws.okx.com:8443/ws/v5/public",
]
BYBIT_REST_HOSTS = ["https://api.bybit.com", "https://api.bytick.com"]
BYBIT_WS_URLS = [
    "wss://stream.bybit.com/v5/public/linear",
    "wss://stream.bytick.com/v5/public/linear",
]

OKX_INST = {"BTC": "BTC-USDT-SWAP", "ETH": "ETH-USDT-SWAP"}
BYBIT_INST = {"BTC": "BTCUSDT", "ETH": "ETHUSDT"}


def _empty_quote(exchange: str) -> dict[str, Any]:
    return {
        "exchange": exchange,
        "price": None,
        "mode": "offline",   # ws | rest | demo | offline
        "simulated": False,
        "ts": None,
    }


exchange_quotes: dict[str, dict[str, dict[str, Any]]] = {
    sym: {ex: _empty_quote(ex) for ex in AGG_EXCHANGES} for sym in AGG_SYMBOLS
}

# 各交易所连接方式：ws 表示实时流在线，用于决定是否需要 REST 降级
exchange_modes: dict[str, str] = {ex: "offline" for ex in AGG_EXCHANGES}

aggregate_state: dict[str, Any] = {"symbols": {}, "updated_at": None}


def update_quote(
    exchange: str,
    symbol: str,
    price: float | None,
    mode: str,
    simulated: bool = False,
) -> None:
    """写入某交易所某币的最新报价。"""
    if symbol not in exchange_quotes or exchange not in exchange_quotes[symbol]:
        return
    if price is None or price <= 0:
        return
    q = exchange_quotes[symbol][exchange]
    # 实时流优先：REST/demo 不覆盖仍然新鲜的 ws 报价
    if q["mode"] == "ws" and mode != "ws" and not _quote_is_stale(q):
        return
    q["price"] = float(price)
    q["mode"] = mode
    q["simulated"] = simulated
    q["ts"] = utc_now_iso()


def _quote_is_stale(q: dict[str, Any], max_age: float = AGG_STALE_SEC) -> bool:
    if not q.get("ts"):
        return True
    try:
        age = (
            datetime.now(timezone.utc) - datetime.fromisoformat(q["ts"])
        ).total_seconds()
    except ValueError:
        return True
    return age > max_age


def compute_aggregate() -> dict[str, Any]:
    """计算全网均价、各所对均价偏离、最高/最低所与跨所价差。"""
    out: dict[str, Any] = {}
    for sym in AGG_SYMBOLS:
        rows = []
        for ex in AGG_EXCHANGES:
            q = exchange_quotes[sym][ex]
            live = q["price"] is not None and not _quote_is_stale(q)
            rows.append(
                {
                    "exchange": ex,
                    "price": q["price"],
                    "mode": q["mode"] if live else "offline",
                    "simulated": q["simulated"],
                    "live": live,
                    "ts": q["ts"],
                }
            )

        valid = [r for r in rows if r["live"]]
        prices = [r["price"] for r in valid]
        avg = sum(prices) / len(prices) if prices else None

        highest = lowest = None
        spread_abs = spread_pct = spread_bps = None
        if len(valid) >= 2 and avg:
            hi = max(valid, key=lambda r: r["price"])
            lo = min(valid, key=lambda r: r["price"])
            highest, lowest = hi["exchange"], lo["exchange"]
            spread_abs = hi["price"] - lo["price"]
            spread_pct = spread_abs / avg * 100
            spread_bps = spread_abs / avg * 10000

        for r in rows:
            if r["live"] and avg:
                r["diff"] = round(r["price"] - avg, 6)
                r["diff_pct"] = round((r["price"] - avg) / avg * 100, 4)
                r["diff_bps"] = round((r["price"] - avg) / avg * 10000, 2)
            else:
                r["diff"] = r["diff_pct"] = r["diff_bps"] = None

        out[sym] = {
            "symbol": sym,
            "rows": rows,
            "avg_price": round(avg, 6) if avg else None,
            "exchange_count": len(valid),
            "highest": highest,
            "lowest": lowest,
            "spread_abs": round(spread_abs, 6) if spread_abs is not None else None,
            "spread_pct": round(spread_pct, 4) if spread_pct is not None else None,
            "spread_bps": round(spread_bps, 2) if spread_bps is not None else None,
            "arb_signal": bool(spread_bps is not None and spread_bps >= AGG_ARB_ALERT_BPS),
        }
    return {"symbols": out, "updated_at": utc_now_iso()}


async def aggregator_loop() -> None:
    """节流广播多交易所价格对比表。"""
    while True:
        await asyncio.sleep(AGG_BROADCAST_SEC)
        try:
            agg = compute_aggregate()
            aggregate_state.update(agg)
            await manager.broadcast(
                {
                    "type": "multi_exchange",
                    "symbols": agg["symbols"],
                    "updated_at": agg["updated_at"],
                    "ts": utc_now_iso(),
                }
            )
        except Exception as exc:
            logger.warning("Aggregator error: %s", exc)


# ---------- OKX ----------
async def okx_ws_loop() -> None:
    """OKX 永续 tickers 实时流，失败退避重连。"""
    backoff = 2
    idx = 0
    while True:
        url = OKX_WS_URLS[idx % len(OKX_WS_URLS)]
        try:
            async with websockets.connect(
                url, ping_interval=20, ping_timeout=20, open_timeout=12
            ) as ws:
                await ws.send(
                    json.dumps(
                        {
                            "op": "subscribe",
                            "args": [
                                {"channel": "tickers", "instId": inst}
                                for inst in OKX_INST.values()
                            ],
                        }
                    )
                )
                exchange_modes["okx"] = "ws"
                backoff = 2
                logger.info("OKX WebSocket connected")
                await manager.broadcast(
                    build_system_event("okx_connected", "OKX 永续实时流已连接")
                )
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    for row in msg.get("data") or []:
                        inst = row.get("instId", "")
                        sym = next(
                            (s for s, i in OKX_INST.items() if i == inst), None
                        )
                        if sym and row.get("last"):
                            update_quote("okx", sym, float(row["last"]), "ws")
        except asyncio.CancelledError:
            exchange_modes["okx"] = "offline"
            raise
        except Exception as exc:
            exchange_modes["okx"] = "offline"
            idx += 1
            logger.warning("OKX WS error: %s (fallback REST)", exc)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


def _okx_rest_price_sync(symbol: str) -> float | None:
    inst = OKX_INST[symbol]
    data = _fetch_json_hosts(f"/api/v5/market/ticker?instId={inst}", OKX_REST_HOSTS)
    if not data:
        return None
    rows = data.get("data") or []
    if not rows or not rows[0].get("last"):
        return None
    return float(rows[0]["last"])


# ---------- Bybit ----------
async def bybit_ws_loop() -> None:
    """Bybit 永续 tickers 实时流（delta 推送，保留上次价格）。"""
    backoff = 2
    idx = 0
    while True:
        url = BYBIT_WS_URLS[idx % len(BYBIT_WS_URLS)]
        try:
            async with websockets.connect(
                url, ping_interval=20, ping_timeout=20, open_timeout=12
            ) as ws:
                await ws.send(
                    json.dumps(
                        {
                            "op": "subscribe",
                            "args": [f"tickers.{i}" for i in BYBIT_INST.values()],
                        }
                    )
                )
                exchange_modes["bybit"] = "ws"
                backoff = 2
                logger.info("Bybit WebSocket connected")
                await manager.broadcast(
                    build_system_event("bybit_connected", "Bybit 永续实时流已连接")
                )
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    topic = msg.get("topic") or ""
                    if not topic.startswith("tickers."):
                        continue
                    pair = topic.split(".", 1)[1]
                    sym = next((s for s, i in BYBIT_INST.items() if i == pair), None)
                    data = msg.get("data") or {}
                    last = data.get("lastPrice") if isinstance(data, dict) else None
                    if sym and last:
                        update_quote("bybit", sym, float(last), "ws")
        except asyncio.CancelledError:
            exchange_modes["bybit"] = "offline"
            raise
        except Exception as exc:
            exchange_modes["bybit"] = "offline"
            idx += 1
            logger.warning("Bybit WS error: %s (fallback REST)", exc)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


def _bybit_rest_price_sync(symbol: str) -> float | None:
    pair = BYBIT_INST[symbol]
    data = _fetch_json_hosts(
        f"/v5/market/tickers?category=linear&symbol={pair}", BYBIT_REST_HOSTS
    )
    if not data:
        return None
    rows = ((data.get("result") or {}).get("list")) or []
    if not rows or not rows[0].get("lastPrice"):
        return None
    return float(rows[0]["lastPrice"])


# ---------- Binance（REST 兜底；WS 由现有现货/合约监听写入）----------
def _binance_rest_price_sync(symbol: str) -> float | None:
    pair = f"{symbol}USDT"
    data = _fetch_json_hosts(
        f"/fapi/v1/ticker/price?symbol={pair}", FUTURES_REST_HOSTS
    )
    if data and data.get("price"):
        return float(data["price"])
    data = _fetch_json_hosts(f"/api/v3/ticker/price?symbol={pair}", SPOT_REST_HOSTS)
    if data and data.get("price"):
        return float(data["price"])
    return None


REST_FETCHERS = {
    "binance": _binance_rest_price_sync,
    "okx": _okx_rest_price_sync,
    "bybit": _bybit_rest_price_sync,
}


async def exchange_rest_fallback_loop() -> None:
    """对未建立实时流的交易所进行 REST 轮询降级。"""
    while True:
        for ex in AGG_EXCHANGES:
            if exchange_modes.get(ex) == "ws":
                continue
            fetcher = REST_FETCHERS[ex]
            for sym in AGG_SYMBOLS:
                try:
                    price = await asyncio.to_thread(fetcher, sym)
                except Exception as exc:
                    logger.debug("%s REST %s error: %s", ex, sym, exc)
                    price = None
                if price:
                    update_quote(ex, sym, price, "rest")
        await asyncio.sleep(AGG_REST_POLL_SEC)


async def exchange_demo_loop() -> None:
    """交易所完全不可达时生成「模拟」报价，保证对比表不空白。

    DEMO_EXCHANGES=off 关闭；=on 始终开启；=auto（默认）仅在该所无有效报价时开启。
    """
    if DEMO_EXCHANGES == "off":
        return
    offsets = {"binance": 0.0, "okx": 0.0004, "bybit": -0.0003}
    while True:
        await asyncio.sleep(AGG_REST_POLL_SEC)
        for sym in AGG_SYMBOLS:
            # 以任一真实报价作为基准价
            base = None
            for ex in AGG_EXCHANGES:
                q = exchange_quotes[sym][ex]
                if q["price"] and not q["simulated"] and not _quote_is_stale(q):
                    base = q["price"]
                    break
            if base is None:
                base = latest_prices.get(sym, {}).get("price")
            if not base:
                continue

            for ex in AGG_EXCHANGES:
                q = exchange_quotes[sym][ex]
                has_real = (
                    q["price"] is not None
                    and not q["simulated"]
                    and not _quote_is_stale(q)
                )
                should_demo = DEMO_EXCHANGES == "on" or (
                    DEMO_EXCHANGES == "auto" and not has_real
                )
                if not should_demo:
                    continue
                drift = offsets.get(ex, 0.0) + random.uniform(-0.0002, 0.0002)
                update_quote(ex, sym, base * (1 + drift), "demo", simulated=True)


def _get_radar_event() -> asyncio.Event:
    global radar_watch_event
    if radar_watch_event is None:
        radar_watch_event = asyncio.Event()
    return radar_watch_event


def ensure_price_slot(symbol: str) -> None:
    if symbol not in latest_prices:
        latest_prices[symbol] = {
            "symbol": symbol,
            "price": None,
            "high_24h": None,
            "low_24h": None,
            "change_24h": None,
            "change_pct_24h": None,
            "volume_24h": None,
            "ts": None,
        }


def _http_get_json(url: str, timeout: float = 20) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "crypto-monitor/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _fetch_json_hosts(path: str, hosts: list[str]) -> Any | None:
    for host in hosts:
        try:
            return _http_get_json(f"{host}{path}")
        except Exception as exc:
            logger.debug("GET %s%s failed: %s", host, path, exc)
            continue
    return None


def _base_from_pair(pair: str) -> str:
    if pair.endswith("USDT"):
        return pair[:-4]
    return pair


def _is_alt_usdt_pair(pair: str) -> bool:
    if not pair.endswith("USDT"):
        return False
    if "_" in pair or pair.endswith("USDT_"):
        return False
    base = _base_from_pair(pair)
    return base not in EXCLUDE_BASES and not base.endswith("UP") and not base.endswith("DOWN")


def _fetch_klines_sync(pair: str, market: str, interval: str, limit: int) -> list | None:
    if market == "futures":
        return _fetch_json_hosts(
            f"/fapi/v1/klines?symbol={pair}&interval={interval}&limit={limit}",
            FUTURES_REST_HOSTS,
        )
    return _fetch_json_hosts(
        f"/api/v3/klines?symbol={pair}&interval={interval}&limit={limit}",
        SPOT_REST_HOSTS,
    )


def _change_1h_from_klines(klines: list) -> float | None:
    """用最近两根 1h K 线估算近 1 小时涨幅(%)。"""
    if not klines or len(klines) < 2:
        return None
    prev_close = float(klines[-2][4])
    last_close = float(klines[-1][4])
    if prev_close <= 0:
        return None
    return (last_close - prev_close) / prev_close * 100.0


def _volatility_1h_from_1m(klines_1m: list) -> float | None:
    """近 60 根 1m K 线的已实现波动（收益标准差 × √60，单位 %）。"""
    if not klines_1m or len(klines_1m) < 10:
        return None
    closes = [float(k[4]) for k in klines_1m]
    rets = []
    for i in range(1, len(closes)):
        if closes[i - 1] > 0:
            rets.append((closes[i] - closes[i - 1]) / closes[i - 1])
    if len(rets) < 5:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / len(rets)
    return (var ** 0.5) * (len(rets) ** 0.5) * 100.0


def _fetch_change_1h_sync(pair: str, market: str) -> float | None:
    data = _fetch_klines_sync(pair, market, "1h", 2)
    if not data:
        return None
    return _change_1h_from_klines(data)


# ---------- 智能短线决策引擎（持仓方向 / 周期 / 动态止盈止损）----------
# 评分阈值：≥ 此分且未被一票否决 → 轻仓做多；否则高危观望
DECISION_LONG_SCORE = 62.0
# 过热：相对开盘已冲太远，禁止追高
DECISION_MAX_EXTENSION_PCT = 2.8
# 15m 已实现波动过高 → 闪击窗口；过低且 1h 趋势稳 → 趋势跟踪
DECISION_FLASH_VOL_15M = 0.85
DECISION_TREND_VOL_15M = 0.45


def _std(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return (sum((x - m) ** 2 for x in xs) / len(xs)) ** 0.5


def _realized_vol_pct(closes: list[float], scale_bars: int | None = None) -> float | None:
    """收盘价序列的已实现波动（%）。scale_bars 默认 = len-1。"""
    if not closes or len(closes) < 8:
        return None
    rets = []
    for i in range(1, len(closes)):
        if closes[i - 1] > 0:
            rets.append((closes[i] - closes[i - 1]) / closes[i - 1])
    if len(rets) < 5:
        return None
    n = scale_bars if scale_bars and scale_bars > 0 else len(rets)
    return _std(rets) * (n ** 0.5) * 100.0


def _atr_pct_from_klines(klines: list, lookback: int = 14) -> float | None:
    """简易 ATR% = mean(TR) / last_close * 100。"""
    if not klines or len(klines) < 3:
        return None
    rows = klines[-(lookback + 1) :]
    trs: list[float] = []
    prev_close = float(rows[0][4])
    for row in rows[1:]:
        high = float(row[2])
        low = float(row[3])
        close = float(row[4])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
        prev_close = close
    if not trs or prev_close <= 0:
        return None
    return (sum(trs) / len(trs)) / prev_close * 100.0


def _extract_microstructure(pair: str, market: str) -> dict[str, Any]:
    """拉取 15m / 1m K 线，提取波动、量能、动量延续、冲高幅度等微观结构。"""
    k15 = _fetch_klines_sync(pair, market, "15m", 24) or []
    k1m = _fetch_klines_sync(pair, market, "1m", 60) or []

    closes_15 = [float(k[4]) for k in k15] if k15 else []
    vols_15 = [float(k[5]) for k in k15] if k15 else []
    closes_1m = [float(k[4]) for k in k1m] if k1m else []
    vols_1m = [float(k[5]) for k in k1m] if k1m else []

    # 真实 15 分钟窗：近 15 根 1m 已实现波动；1h 窗沿用近 60 根 1m
    vol_15m = _realized_vol_pct(closes_1m[-16:], scale_bars=15) if len(closes_1m) >= 16 else None
    if vol_15m is None and closes_15:
        # 退化：用近 4 根 15m（约 1h）收益波动，再缩放到单根 15m 量级
        vol_1h_from_15 = _realized_vol_pct(closes_15[-8:], scale_bars=4)
        vol_15m = (vol_1h_from_15 / 2.0) if vol_1h_from_15 is not None else None
    vol_1h = _volatility_1h_from_1m(k1m) if k1m else None
    atr_15m = _atr_pct_from_klines(k15, 14) if k15 else None

    # 量能脉冲：近 4 根 15m 成交量 / 前 12 根均值
    volume_pulse = None
    if len(vols_15) >= 16:
        recent = sum(vols_15[-4:]) / 4
        base = sum(vols_15[-16:-4]) / 12
        if base > 0:
            volume_pulse = recent / base
    elif len(vols_1m) >= 40:
        recent = sum(vols_1m[-10:]) / 10
        base = sum(vols_1m[-40:-10]) / 30
        if base > 0:
            volume_pulse = recent / base

    # 动量延续：近 N 根收阳占比 + 收盘相对均线位置
    green_ratio_15 = None
    if len(k15) >= 8:
        window = k15[-8:]
        greens = sum(1 for k in window if float(k[4]) >= float(k[1]))
        green_ratio_15 = greens / len(window)

    # 冲高幅度：现价相对近 1h 开盘（前 4 根 15m 开盘）
    extension_pct = None
    if len(k15) >= 4:
        open_ref = float(k15[-4][1])
        last = float(k15[-1][4])
        if open_ref > 0:
            extension_pct = (last - open_ref) / open_ref * 100.0

    # 15m 与 1h 方向对齐：近 1 根 15m 涨跌 vs 近 1h 涨跌同号
    change_15m = None
    if len(k15) >= 2:
        a, b = float(k15[-2][4]), float(k15[-1][4])
        if a > 0:
            change_15m = (b - a) / a * 100.0

    # 最近一根是否长上影（冲高回落信号）
    upper_wick_ratio = None
    if k15:
        o, h, l, c = float(k15[-1][1]), float(k15[-1][2]), float(k15[-1][3]), float(k15[-1][4])
        rng = h - l
        if rng > 0:
            upper_wick_ratio = (h - max(o, c)) / rng

    return {
        "vol_15m": vol_15m,
        "vol_1h": vol_1h,
        "atr_15m": atr_15m,
        "volume_pulse": volume_pulse,
        "green_ratio_15": green_ratio_15,
        "extension_pct": extension_pct,
        "change_15m": change_15m,
        "upper_wick_ratio": upper_wick_ratio,
    }


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def build_trade_decision(
    coin: dict[str, Any],
    safety: dict[str, Any],
    *,
    market: str = "spot",
    micro: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """多因子短线决策：方向 + 持仓窗口 + 动态止盈/止损。

    因子（满分约 100，再经否决项裁剪）：
    1. 相对强度 vs BTC（核心）
    2. 15m 量能脉冲（确认有资金进场）
    3. 动量延续（阳线占比，避免单根脉冲）
    4. 波动适中（过低无利润空间；过高难控回撤）
    5. 未过度延伸（防追高）
    6. 大盘环境（禁多 / 偏弱扣分）
    """
    vs_btc = float(coin.get("vs_btc_1h") or 0.0)
    change_1h = float(coin.get("change_1h") or 0.0)
    funding = coin.get("funding_rate")
    no_long = bool(safety.get("no_long"))
    btc_trend = safety.get("btc_trend") or "flat"
    btc_vol = float(safety.get("btc_volatility_1h") or 0.0)

    if micro is None:
        pair = coin.get("pair") or f"{coin.get('symbol')}USDT"
        try:
            micro = _extract_microstructure(pair, market)
        except Exception as exc:
            logger.debug("microstructure extract failed %s: %s", pair, exc)
            micro = {}

    vol_15m = micro.get("vol_15m")
    vol_1h = micro.get("vol_1h")
    atr_15m = micro.get("atr_15m")
    volume_pulse = micro.get("volume_pulse")
    green_ratio = micro.get("green_ratio_15")
    extension = micro.get("extension_pct")
    change_15m = micro.get("change_15m")
    upper_wick = micro.get("upper_wick_ratio")

    score = 0.0
    reasons: list[str] = []
    vetoes: list[str] = []

    # --- 1) 相对强度（0-28）---
    if vs_btc >= 1.2:
        score += 28
        reasons.append(f"相对BTC极强 +{vs_btc:.2f}%")
    elif vs_btc >= 0.6:
        score += 22
        reasons.append(f"相对BTC偏强 +{vs_btc:.2f}%")
    elif vs_btc >= 0.25:
        score += 14
        reasons.append(f"相对BTC略强 +{vs_btc:.2f}%")
    elif vs_btc > 0:
        score += 6
        reasons.append(f"相对BTC微弱领先 +{vs_btc:.2f}%")
    else:
        vetoes.append("未跑赢BTC")

    # --- 2) 量能脉冲（0-18）---
    if volume_pulse is not None:
        if volume_pulse >= 1.8:
            score += 18
            reasons.append(f"量能脉冲×{volume_pulse:.2f}")
        elif volume_pulse >= 1.25:
            score += 12
            reasons.append(f"量能温和放大×{volume_pulse:.2f}")
        elif volume_pulse >= 0.9:
            score += 6
        else:
            score -= 8
            reasons.append(f"量能萎缩×{volume_pulse:.2f}")
            if volume_pulse < 0.7:
                vetoes.append("量能明显萎缩")

    # --- 3) 动量延续（0-16）---
    if green_ratio is not None:
        if green_ratio >= 0.75:
            score += 16
            reasons.append(f"15m阳线占比{green_ratio*100:.0f}%")
        elif green_ratio >= 0.55:
            score += 10
            reasons.append(f"15m动量尚可")
        elif green_ratio >= 0.4:
            score += 4
        else:
            score -= 6
            reasons.append("15m阳线偏少，动量不稳")

    # 15m / 1h 同向加分
    if change_15m is not None and change_1h != 0:
        if (change_15m > 0 and change_1h > 0) or (change_15m < 0 and change_1h < 0):
            score += 6
            reasons.append("15m与1h方向对齐")
        else:
            score -= 8
            reasons.append("15m与1h背离")
            if change_1h > 0 and change_15m < -0.35:
                vetoes.append("短周期回撤背离")

    # --- 4) 波动适中（0-14，过高/过低都扣）---
    v15 = float(vol_15m) if vol_15m is not None else None
    if v15 is not None:
        if 0.35 <= v15 <= 1.6:
            score += 14
            reasons.append(f"15m波动适中{v15:.2f}%")
        elif 0.2 <= v15 < 0.35 or 1.6 < v15 <= 2.4:
            score += 6
            reasons.append(f"15m波动偏边缘{v15:.2f}%")
        elif v15 > 2.4:
            score -= 10
            vetoes.append(f"15m波动过高{v15:.2f}%")
        else:
            score -= 4
            reasons.append("波动过低，利润空间不足")

    # --- 5) 冲高/上影否决（防追高）---
    if extension is not None:
        if extension >= DECISION_MAX_EXTENSION_PCT:
            vetoes.append(f"1h内已冲高{extension:.2f}%，追高风险")
            score -= 18
        elif extension >= 1.8:
            score -= 8
            reasons.append(f"冲高偏多{extension:.2f}%，宜等回踩")
        elif 0.3 <= extension <= 1.5:
            score += 6
            reasons.append("涨幅未过度延伸")

    if upper_wick is not None and upper_wick >= 0.55:
        score -= 10
        reasons.append("长上影冲高回落")
        if upper_wick >= 0.7:
            vetoes.append("长上影拒绝信号")

    # --- 6) 费率 / 大盘环境 ---
    if funding is not None:
        try:
            fr = abs(float(funding))
            if fr > MAX_ABS_FUNDING:
                vetoes.append(f"费率过热{fr*100:.3f}%")
            elif fr > MAX_ABS_FUNDING * 0.7:
                score -= 6
                reasons.append("费率偏热，仓位需更轻")
            else:
                score += 4
        except (TypeError, ValueError):
            pass

    if no_long:
        vetoes.append("全局禁多令")
        score = min(score, 20)
    elif btc_trend == "down":
        score -= 12
        reasons.append("大盘偏空，山寨跟涨可靠性下降")
    elif btc_trend == "up":
        score += 6
        reasons.append("大盘偏多共振")

    if btc_vol >= BTC_HIGH_VOL_1H:
        score -= 8
        reasons.append("大盘波动偏高")

    score = _clamp(score, 0, 100)

    # --- 动态止盈 / 止损（ATR 驱动，锁定盈亏比）---
    atr = float(atr_15m) if atr_15m and atr_15m > 0 else (v15 if v15 else 0.7)
    # 止损：约 0.7~1.0×ATR，夹在 0.4%~1.2%
    stop_pct = _clamp(round(atr * 0.85, 2), 0.4, 1.2)
    # 止盈：目标 R:R ≥ 1.5，夹在 0.7%~2.5%
    take_pct = _clamp(round(max(stop_pct * 1.6, atr * 1.25), 2), 0.7, 2.5)
    if take_pct / stop_pct < 1.4:
        take_pct = _clamp(round(stop_pct * 1.5, 2), 0.7, 2.5)
    rr = round(take_pct / stop_pct, 2) if stop_pct else None

    # --- 持仓窗口 ---
    # 高 15m 波动 + 量能脉冲 → 闪击；波动收敛 + 相对强度持续 → 趋势
    if v15 is not None and v15 >= DECISION_FLASH_VOL_15M and (volume_pulse or 1) >= 1.2:
        horizon = "flash"
        horizon_label = "短线闪击 (15m-1h)"
    elif v15 is not None and v15 <= DECISION_TREND_VOL_15M and vs_btc >= 0.4:
        horizon = "trend"
        horizon_label = "趋势跟踪 (1h-4h)"
    elif vs_btc >= 0.8 and (volume_pulse or 0) >= 1.3:
        horizon = "flash"
        horizon_label = "短线闪击 (15m-1h)"
    else:
        horizon = "swing"
        horizon_label = "波段观察 (1h-2h)"

    # --- 方向判定 ---
    hard_veto = bool(vetoes) or no_long
    can_long = (not hard_veto) and score >= DECISION_LONG_SCORE and vs_btc > 0
    # 盈亏比过差也不给多
    if rr is not None and rr < 1.3:
        can_long = False
        vetoes.append(f"盈亏比不足({rr})")

    if can_long:
        direction = "long_light"
        direction_label = "轻仓做多"
        confidence = "high" if score >= 75 else "medium"
    else:
        direction = "watch"
        direction_label = "高危观望"
        confidence = "low" if score < 45 else "medium"

    summary_bits = reasons[:3]
    if vetoes:
        summary_bits = vetoes[:2] + summary_bits[:1]

    return {
        "direction": direction,
        "direction_label": direction_label,
        "horizon": horizon,
        "horizon_label": horizon_label,
        "take_profit_pct": take_pct,
        "stop_loss_pct": stop_pct,
        "risk_reward": rr,
        "score": round(score, 1),
        "confidence": confidence,
        "features": {
            "vol_15m": round(vol_15m, 4) if vol_15m is not None else None,
            "vol_1h": round(vol_1h, 4) if vol_1h is not None else None,
            "atr_15m": round(atr_15m, 4) if atr_15m is not None else None,
            "volume_pulse": round(volume_pulse, 3) if volume_pulse is not None else None,
            "green_ratio_15": round(green_ratio, 3) if green_ratio is not None else None,
            "extension_pct": round(extension, 3) if extension is not None else None,
            "change_15m": round(change_15m, 4) if change_15m is not None else None,
        },
        "reason": " · ".join(summary_bits) if summary_bits else "因子中性",
        "vetoes": vetoes,
    }


def _sma(values: list[float], period: int) -> float | None:
    if len(values) < period or period <= 0:
        return None
    window = values[-period:]
    return sum(window) / period


def _bollinger(closes: list[float], period: int = 20, mult: float = 2.0) -> dict[str, float | None]:
    mid = _sma(closes, period)
    if mid is None:
        return {"mid": None, "upper": None, "lower": None}
    window = closes[-period:]
    var = sum((c - mid) ** 2 for c in window) / period
    std = var ** 0.5
    return {
        "mid": mid,
        "upper": mid + mult * std,
        "lower": mid - mult * std,
    }


def _swing_low(klines: list, lookback: int = 20) -> float | None:
    if not klines:
        return None
    lows = [float(k[3]) for k in klines[-lookback:]]
    return min(lows) if lows else None


def _swing_high(klines: list, lookback: int = 20) -> float | None:
    if not klines:
        return None
    highs = [float(k[2]) for k in klines[-lookback:]]
    return max(highs) if highs else None


def _round_price(price: float) -> float:
    """动态小数位舍入（展示用）；盈亏比等计算应使用舍入前的高精度值。"""
    from simlab.price_format import round_price

    return round_price(float(price))


def calculate_advanced_trading_levels(
    df_4h: pd.DataFrame, df_1h: pd.DataFrame, df_15m: pd.DataFrame
) -> dict:
    """多周期量化挂单点位（4h 防线 · 1h 布林下轨 · 15m ATR/量能修正）。"""
    # 1. 4h 绝对防线 (Defense)
    defense = float(df_4h["low"].tail(20).min())

    # 2. 1h 布林下轨支撑 (Lower Band)
    sma_1h = df_1h["close"].rolling(20).mean()
    std_1h = df_1h["close"].rolling(20).std()
    lower_band_1h = sma_1h - 2 * std_1h
    current_lower_band = float(lower_band_1h.iloc[-1])

    # 3. 15m 波动率 (ATR) 与成交量特征
    high_15m = df_15m["high"]
    low_15m = df_15m["low"]
    close_15m = df_15m["close"]
    volume_15m = (
        df_15m["volume"]
        if "volume" in df_15m.columns
        else pd.Series(1.0, index=df_15m.index)
    )

    tr = pd.concat(
        [
            high_15m - low_15m,
            (high_15m - close_15m.shift()).abs(),
            (low_15m - close_15m.shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr_15m = tr.rolling(window=14).mean()
    current_atr = float(atr_15m.iloc[-1])

    recent_low_15m = float(low_15m.tail(5).min())

    vol_mean_5 = float(volume_15m.tail(5).mean())
    vol_mean_20 = float(volume_15m.rolling(20).mean().iloc[-1])
    is_panic_volume = vol_mean_5 > (1.5 * vol_mean_20) if vol_mean_20 > 0 else False
    atr_coeff = 0.5 if is_panic_volume else 0.3

    # 4. 核心点位计算与硬性风控修正
    raw_entry = min(current_lower_band, recent_low_15m) - atr_coeff * current_atr

    if raw_entry < defense:
        entry = defense * 1.002
    else:
        entry = raw_entry

    raw_stop_loss = entry - 1.5 * current_atr
    min_allowed_stop = defense * 0.99
    if raw_stop_loss < min_allowed_stop:
        stop_loss = min_allowed_stop
    else:
        stop_loss = raw_stop_loss

    take_profit = entry * 1.010

    return {
        "defense": float(defense),
        "lower_band": float(current_lower_band),
        "entry": float(entry),
        "stop_loss": float(stop_loss),
        "take_profit": float(take_profit),
        "atr": float(current_atr),
        "panic_volume": bool(is_panic_volume),
        "atr_coeff": atr_coeff,
    }


def _binance_klines_to_df(raw: list | None) -> pd.DataFrame:
    """币安 K 线数组 → DataFrame(open/high/low/close/volume)。"""
    rows = []
    for k in raw or []:
        try:
            rows.append(
                {
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "volume": float(k[5]),
                }
            )
        except (TypeError, ValueError, IndexError):
            continue
    return pd.DataFrame(rows)


def _fetch_okx_swap_bases_sync() -> set[str]:
    data = _fetch_json_hosts(
        "/api/v5/public/instruments?instType=SWAP", OKX_REST_HOSTS
    )
    bases: set[str] = set()
    for row in (data or {}).get("data") or []:
        if row.get("state") != "live":
            continue
        if (row.get("settleCcy") or "").upper() != "USDT":
            continue
        inst = row.get("instId") or ""
        # BTC-USDT-SWAP → BTC
        parts = inst.split("-")
        if len(parts) >= 2 and parts[1] == "USDT":
            base = parts[0].upper()
            if base and base not in EXCLUDE_BASES:
                bases.add(base)
    return bases


def _fetch_binance_perp_bases_sync() -> tuple[set[str], str]:
    """返回 (bases, source)。合约不可达时退化到现货 USDT 交易对作代理。"""
    info = _fetch_json_hosts("/fapi/v1/exchangeInfo", FUTURES_REST_HOSTS)
    bases: set[str] = set()
    if info and info.get("symbols"):
        for s in info["symbols"]:
            if s.get("contractType") and s.get("contractType") != "PERPETUAL":
                continue
            if s.get("quoteAsset") != "USDT":
                continue
            if s.get("status") not in (None, "TRADING"):
                continue
            sym = s.get("symbol") or ""
            if _is_alt_usdt_pair(sym):
                bases.add(_base_from_pair(sym))
        if bases:
            return bases, "binance_perp"

    # 现货代理：至少保证「币安有现货、OKX 有永续」的交集可用
    info = _fetch_json_hosts("/api/v3/exchangeInfo", SPOT_REST_HOSTS)
    for s in (info or {}).get("symbols") or []:
        if s.get("status") != "TRADING":
            continue
        if s.get("quoteAsset") != "USDT":
            continue
        sym = s.get("symbol") or ""
        if _is_alt_usdt_pair(sym):
            bases.add(_base_from_pair(sym))
    return bases, "binance_spot_proxy"


def get_twin_star_bases() -> tuple[set[str], dict[str, Any]]:
    """币安永续 ∩ OKX 永续白名单（带缓存）。"""
    now = time.time()
    if (
        _twin_star_cache["bases"]
        and now - float(_twin_star_cache["ts"]) < TWIN_STAR_CACHE_SEC
    ):
        return set(_twin_star_cache["bases"]), dict(_twin_star_cache["meta"])

    okx_bases = _fetch_okx_swap_bases_sync()
    bin_bases, bin_src = _fetch_binance_perp_bases_sync()
    twin = okx_bases & bin_bases
    meta = {
        "okx_count": len(okx_bases),
        "binance_count": len(bin_bases),
        "twin_count": len(twin),
        "binance_source": bin_src,
    }
    _twin_star_cache["ts"] = now
    _twin_star_cache["bases"] = twin
    _twin_star_cache["meta"] = meta
    logger.info(
        "Twin-star whitelist: OKX=%s Binance(%s)=%s ∩=%s",
        meta["okx_count"],
        bin_src,
        meta["binance_count"],
        meta["twin_count"],
    )
    return set(twin), meta


def _fetch_okx_funding_sync(inst_id: str) -> float | None:
    data = _fetch_json_hosts(
        f"/api/v5/public/funding-rate?instId={inst_id}", OKX_REST_HOSTS
    )
    rows = (data or {}).get("data") or []
    if not rows:
        return None
    try:
        return float(rows[0].get("fundingRate"))
    except (TypeError, ValueError):
        return None


def _composite_sniper_score(c: dict[str, Any]) -> float:
    """综合狙击分：相对强度 + 流动性 + 费率健康 + 方向。"""
    vs = float(c.get("vs_btc_1h") or 0.0)
    vol = float(c.get("quote_volume") or 0.0)
    fr = c.get("funding_rate")
    ch = float(c.get("change_1h") or 0.0)

    # 相对强度：0~2% 映射到 0~40
    strength = min(max(vs, 0.0), 2.0) / 2.0 * 40.0
    # 流动性：5e7~5e8 映射到 0~30
    if vol <= 0:
        liq = 0.0
    else:
        liq = min(max(np.log10(vol) - np.log10(MIN_QUOTE_VOLUME), 0.0), 1.0) * 30.0
    # 费率：越接近 0 越好，0~20
    if fr is None:
        fund = 10.0
    else:
        fund = max(0.0, 1.0 - abs(float(fr)) / MAX_ABS_FUNDING) * 20.0
    direction = 10.0 if ch > 0 else 0.0
    return round(strength + liq + fund + direction, 2)


def _tf_structure(klines: list) -> dict[str, Any]:
    """单周期：收盘、SMA20/50、布林带、摆动高低。"""
    if not klines or len(klines) < 25:
        return {}
    closes = [float(k[4]) for k in klines]
    last = closes[-1]
    sma20 = _sma(closes, 20)
    sma50 = _sma(closes, 50) if len(closes) >= 50 else _sma(closes, 30)
    bb = _bollinger(closes, 20, 2.0)
    return {
        "close": last,
        "sma20": sma20,
        "sma50": sma50,
        "bb_mid": bb["mid"],
        "bb_upper": bb["upper"],
        "bb_lower": bb["lower"],
        "swing_low": _swing_low(klines, 20),
        "swing_high": _swing_high(klines, 20),
    }


def _regime_4h(s4: dict[str, Any]) -> tuple[str, str]:
    """4h 定大方向：trend_long / range / trend_short。"""
    if not s4:
        return "range", "4h 数据不足，按震荡处理"
    c = s4.get("close") or 0
    s20 = s4.get("sma20")
    s50 = s4.get("sma50")
    mid = s4.get("bb_mid")
    if s20 and s50 and c > s20 >= s50:
        return "trend_long", "4h 多头排列（价>MA20≥MA50）"
    if s20 and s50 and c < s20 <= s50:
        return "trend_short", "4h 空头排列（价<MA20≤MA50）"
    if mid and s20 and abs(c - mid) / mid < 0.015 and abs(s20 - (s50 or s20)) / s20 < 0.01:
        return "range", "4h 布林中轨附近震荡"
    if s20 and c > s20:
        return "trend_long", "4h 站上 MA20，偏多"
    if s20 and c < s20:
        return "trend_short", "4h 跌破 MA20，偏空"
    return "range", "4h 结构中性震荡"


def build_order_levels(
    coin: dict[str, Any],
    decision: dict[str, Any],
    *,
    market: str = "spot",
    simulated: bool = False,
) -> dict[str, Any]:
    """多周期量化挂单：优先调用 calculate_advanced_trading_levels 纯函数。"""
    price = float(coin.get("price") or 0)
    horizon = decision.get("horizon") or "swing"
    direction = decision.get("direction") or "watch"
    pair = coin.get("pair") or f"{coin.get('symbol')}USDT"

    if simulated or price <= 0:
        entry = _round_price(price * 0.997) if price else None
        stop = _round_price(entry * 0.99) if entry else None
        take = _round_price(entry * 1.01) if entry else None
        return {
            "regime_4h": "range",
            "regime_4h_label": "演示 · 震荡假设",
            "core_support_1h": entry,
            "micro_support_15m": entry,
            "suggest_interval": "15m",
            "suggest_interval_label": "15m 回踩挂单（演示）",
            "entry_price": entry,
            "stop_price": stop,
            "take_profit_price": take,
            "defense": stop,
            "lower_band": entry,
            "atr": None,
            "valid_for_long": direction == "long_light" and entry is not None,
            "note": "演示挂单点位",
            "levels": {},
            "engine": "demo",
        }

    # K 线：优先现货（本网络可达）；合约作补充
    k15 = _fetch_klines_sync(pair, "spot", "15m", 80) or _fetch_klines_sync(
        pair, market, "15m", 80
    )
    k1h = _fetch_klines_sync(pair, "spot", "1h", 80) or _fetch_klines_sync(
        pair, market, "1h", 80
    )
    k4h = _fetch_klines_sync(pair, "spot", "4h", 80) or _fetch_klines_sync(
        pair, market, "4h", 80
    )

    df_15m = _binance_klines_to_df(k15)
    df_1h = _binance_klines_to_df(k1h)
    df_4h = _binance_klines_to_df(k4h)

    advanced = None
    if len(df_15m) >= 25 and len(df_1h) >= 25 and len(df_4h) >= 25:
        try:
            advanced = calculate_advanced_trading_levels(df_4h, df_1h, df_15m)
        except Exception as exc:
            logger.warning("advanced levels failed %s: %s", pair, exc)

    s15 = _tf_structure(k15 or [])
    s1h = _tf_structure(k1h or [])
    s4h = _tf_structure(k4h or [])
    regime, regime_label = _regime_4h(s4h)
    if s15.get("close"):
        price = float(s15["close"])

    if advanced:
        entry = _round_price(float(advanced["entry"]))
        stop = _round_price(float(advanced["stop_loss"]))
        take = _round_price(float(advanced["take_profit"]))
        defense = _round_price(float(advanced["defense"]))
        lower_band = _round_price(float(advanced["lower_band"]))
        atr = float(advanced["atr"])
        suggest_iv = "15m"
        suggest_iv_label = (
            "15m ATR 回踩挂单"
            + (" · 放量修正" if advanced.get("panic_volume") else "")
        )
        note_parts = [
            regime_label,
            f"4h防线 {defense}",
            f"1h布林下轨 {lower_band}",
            f"ATR {atr}",
        ]
        engine = "advanced_mtf"
    else:
        # 退化到旧结构算法
        candidates_1h = [
            v
            for v in (s1h.get("bb_lower"), s1h.get("sma20"), s1h.get("swing_low"))
            if v is not None and v < price
        ]
        core_1h = max(candidates_1h) if candidates_1h else price * 0.992
        candidates_15 = [
            v
            for v in (s15.get("bb_lower"), s15.get("sma20"), s15.get("swing_low"))
            if v is not None and v < price
        ]
        micro_15 = max(candidates_15) if candidates_15 else core_1h
        entry = _round_price(min(0.5 * micro_15 + 0.5 * core_1h, price * 0.9985))
        stop = _round_price(min(core_1h * 0.995, entry * 0.994))
        take = _round_price(entry * 1.01)
        defense = _round_price(float(s4h.get("swing_low") or stop))
        lower_band = _round_price(float(s1h.get("bb_lower") or entry))
        atr = None
        suggest_iv = "1h"
        suggest_iv_label = "结构回踩（高级算法数据不足）"
        note_parts = [regime_label, "高级点位算法数据不足，已降级"]
        engine = "legacy_structure"

    valid_for_long = (
        direction == "long_light"
        and regime != "trend_short"
        and entry is not None
        and stop is not None
        and take is not None
        and stop < entry < take
    )
    if not valid_for_long:
        if direction != "long_light":
            note_parts.append("决策非做多，点位仅供观察")
        elif regime == "trend_short":
            note_parts.append("4h 偏空，禁止挂多")

    def _pack(s: dict[str, Any]) -> dict[str, Any]:
        return {
            k: (_round_price(v) if isinstance(v, float) else v) for k, v in s.items()
        }

    return {
        "regime_4h": regime,
        "regime_4h_label": regime_label,
        "core_support_1h": lower_band,
        "micro_support_15m": entry,
        "suggest_interval": suggest_iv,
        "suggest_interval_label": suggest_iv_label,
        "entry_price": entry,
        "stop_price": stop,
        "take_profit_price": take,
        "defense": defense,
        "lower_band": lower_band,
        "atr": atr,
        "valid_for_long": valid_for_long,
        "note": " · ".join(note_parts),
        "levels": {"15m": _pack(s15), "1h": _pack(s1h), "4h": _pack(s4h)},
        "engine": engine,
        "advanced": advanced,
    }


def _attach_decision(
    item: dict[str, Any],
    safety: dict[str, Any],
    *,
    market: str,
    simulated: bool = False,
) -> dict[str, Any]:
    """给雷达条目挂上决策字段 + 多周期挂单点位。"""
    if simulated:
        # 演示：用已有涨幅伪造一个可解释决策，避免额外网络请求
        vs = float(item.get("vs_btc_1h") or 0.5)
        decision = {
            "direction": "long_light" if vs >= 0.4 and not safety.get("no_long") else "watch",
            "direction_label": "轻仓做多" if vs >= 0.4 and not safety.get("no_long") else "高危观望",
            "horizon": "flash" if vs >= 0.9 else "trend",
            "horizon_label": "短线闪击 (15m-1h)" if vs >= 0.9 else "趋势跟踪 (1h-4h)",
            "take_profit_pct": 1.2 if vs >= 0.9 else 1.0,
            "stop_loss_pct": 0.7 if vs >= 0.9 else 0.6,
            "risk_reward": round((1.2 if vs >= 0.9 else 1.0) / (0.7 if vs >= 0.9 else 0.6), 2),
            "score": 70.0 if vs >= 0.4 else 40.0,
            "confidence": "medium",
            "features": {},
            "reason": "演示决策（非实盘微观结构）",
            "vetoes": [],
        }
    else:
        decision = build_trade_decision(item, safety, market=market)

    try:
        orders = build_order_levels(
            item, decision, market=market, simulated=simulated
        )
    except Exception as exc:
        logger.warning("order levels failed for %s: %s", item.get("symbol"), exc)
        orders = {
            "regime_4h": "range",
            "regime_4h_label": "测算失败",
            "core_support_1h": None,
            "micro_support_15m": None,
            "suggest_interval": "1h",
            "suggest_interval_label": "—",
            "entry_price": None,
            "stop_price": None,
            "take_profit_price": None,
            "valid_for_long": False,
            "note": str(exc),
            "levels": {},
        }

    item["decision"] = decision
    item["orders"] = orders
    item["suggest_direction"] = decision["direction"]
    item["suggest_direction_label"] = decision["direction_label"]
    item["suggest_horizon"] = decision["horizon"]
    item["suggest_horizon_label"] = decision["horizon_label"]
    item["suggest_take_profit_pct"] = decision["take_profit_pct"]
    item["suggest_stop_loss_pct"] = decision["stop_loss_pct"]
    item["suggest_risk_reward"] = decision["risk_reward"]
    item["suggest_score"] = decision["score"]
    item["suggest_reason"] = decision["reason"]
    # 挂单扁平字段
    item["order_interval"] = orders.get("suggest_interval")
    item["order_interval_label"] = orders.get("suggest_interval_label")
    item["order_regime_4h"] = orders.get("regime_4h")
    item["order_regime_label"] = orders.get("regime_4h_label")
    item["order_entry"] = orders.get("entry_price")
    item["order_stop"] = orders.get("stop_price")
    item["order_take"] = orders.get("take_profit_price")
    item["order_valid"] = orders.get("valid_for_long")
    item["order_note"] = orders.get("note")
    item["order_support_1h"] = orders.get("core_support_1h")
    item["order_support_15m"] = orders.get("micro_support_15m")
    item["order_defense"] = orders.get("defense")
    item["order_lower_band"] = orders.get("lower_band")
    item["order_atr"] = orders.get("atr")
    item["order_engine"] = orders.get("engine")
    return item


def _apply_ambush_ranking(items: list[dict[str, Any]]) -> dict[str, Any]:
    """用综合评分引擎重排 TOP10，并产出「推荐前三强」。"""
    if not items or rank_ambush_rotation is None:
        return {
            "items": items,
            "top3": [],
            "top3_fallback": False,
            "passed_count": 0,
        }

    prepared: list[dict[str, Any]] = []
    for it in items:
        entry = it.get("order_entry")
        stop = it.get("order_stop")
        take = it.get("order_take")
        if not entry or not stop or not take:
            continue
        # 补充 15m 量能比（硬过滤用）
        vol_ratio = None
        try:
            k15 = _fetch_klines_sync(it.get("pair") or f"{it.get('symbol')}USDT", "spot", "15m", 30) or []
            if len(k15) >= 20:
                vols = [float(k[5]) for k in k15]
                base = sum(vols[-20:]) / 20.0
                recent = sum(vols[-5:]) / 5.0
                if base > 0:
                    vol_ratio = recent / base
        except Exception:
            vol_ratio = None
        prepared.append(
            {
                **it,
                "vs_btc": it.get("vs_btc_1h") or it.get("vs_btc_24h") or 0,
                "levels": {
                    "entry": float(entry),
                    "stop_loss": float(stop),
                    "take_profit": float(take),
                    "atr": it.get("order_atr"),
                },
                "vol_ratio_15m": vol_ratio,
            }
        )

    if not prepared:
        return {
            "items": items,
            "top3": [],
            "top3_fallback": False,
            "passed_count": 0,
        }

    ranked = rank_ambush_rotation(prepared)
    by_sym = {it["symbol"]: it for it in items}
    merged: list[dict[str, Any]] = []
    for r in ranked.get("top10") or []:
        base = dict(by_sym.get(r["symbol"]) or {})
        base.update(
            {
                "rank": r.get("rank"),
                "total_score": r.get("total_score"),
                "composite_score": r.get("total_score"),
                "score_volume": r.get("score_volume"),
                "score_rel_strength": r.get("score_rel_strength"),
                "score_funding": r.get("score_funding"),
                "score_volatility": r.get("score_volatility"),
                "score_operability": r.get("score_operability"),
                "hard_pass": r.get("hard_pass"),
                "hard_fail_reasons": r.get("hard_fail_reasons") or [],
                "distance_pct": r.get("distance_pct"),
                "first_entry_distance_pct": r.get("first_entry_distance_pct"),
                "risk_distance": r.get("risk_distance"),
                "risk_reward_ratio": r.get("risk_reward_ratio"),
                "atr_pct": r.get("atr_pct"),
                "tranche_1_price": r.get("tranche_1_price"),
                "tranche_2_price": r.get("tranche_2_price"),
                "tranche_gap_pct": r.get("tranche_gap_pct"),
                "stop_gap_pct": r.get("stop_gap_pct"),
                "ladder_valid": r.get("ladder_valid"),
                "batch_orders": r.get("batch_orders"),
            }
        )
        # 用安全层修正后的点位覆盖表格展示字段，避免与前三强口径不一致
        if r.get("entry") and r.get("stop_loss") and r.get("take_profit"):
            base["order_entry"] = _round_price(float(r["entry"]))
            base["order_stop"] = _round_price(float(r["stop_loss"]))
            base["order_take"] = _round_price(float(r["take_profit"]))
            base["order_valid"] = bool(r.get("ladder_valid")) and base.get("order_valid", True)
        merged.append(base)

    # 未进入评分的币种追加在末尾
    seen = {m["symbol"] for m in merged}
    for it in items:
        if it["symbol"] not in seen:
            row = dict(it)
            row["hard_pass"] = False
            row["hard_fail_reasons"] = row.get("hard_fail_reasons") or ["缺少有效点位"]
            merged.append(row)

    for i, row in enumerate(merged, 1):
        row["rank"] = i

    return {
        "items": merged[:RADAR_TOP_N],
        "top3": ranked.get("top3") or [],
        "top3_fallback": bool(ranked.get("top3_fallback")),
        "passed_count": int(ranked.get("passed_count") or 0),
    }


def evaluate_market_safety_sync(market: str = "spot") -> dict[str, Any]:
    """计算 BTC 1h 涨跌与波动，判定是否触发全局禁多令。"""
    change = _fetch_change_1h_sync("BTCUSDT", market)
    k1m = _fetch_klines_sync("BTCUSDT", market, "1m", 60)
    vol = _volatility_1h_from_1m(k1m) if k1m else None

    if change is None:
        change = 0.0
    if vol is None:
        # 退化：用 1h K 线振幅近似
        k1h = _fetch_klines_sync("BTCUSDT", market, "1h", 1)
        if k1h:
            high = float(k1h[-1][2])
            low = float(k1h[-1][3])
            close = float(k1h[-1][4]) or 1
            vol = (high - low) / close * 100.0
        else:
            vol = 0.0

    if change <= -0.35:
        trend = "down"
    elif change >= 0.35:
        trend = "up"
    else:
        trend = "flat"

    no_long = False
    reason = ""
    if change <= BTC_CRASH_CHANGE_1H:
        no_long = True
        reason = f"BTC 1h 暴跌 {change:.2f}% ≤ {BTC_CRASH_CHANGE_1H}%，触发全局禁多令"
    elif change <= BTC_SOFT_CRASH_1H and vol >= BTC_HIGH_VOL_1H:
        no_long = True
        reason = (
            f"BTC 1h 下跌 {change:.2f}% 且波动 {vol:.2f}% 偏高，"
            f"触发全局禁多令"
        )
    else:
        reason = f"BTC 1h {change:+.2f}% · 波动 {vol:.2f}% · 环境允许低杠杆做多"

    safety = {
        "status": "risk" if no_long else "safe",
        "no_long": no_long,
        "label": "大盘风险避险中" if no_long else "安全可做多",
        "btc_change_1h": round(change, 4),
        "btc_volatility_1h": round(vol, 4),
        "btc_trend": trend,
        "reason": reason,
        "updated_at": utc_now_iso(),
    }
    return enrich_alt_rotation_advice(safety)


def _btc_funding_healthy() -> bool | None:
    """BTC 资金费率是否健康。None 表示暂无费率数据。"""
    fr = (futures_data.get("BTC") or {}).get("funding_rate")
    if fr is None:
        return None
    try:
        return abs(float(fr)) <= MAX_ABS_FUNDING
    except (TypeError, ValueError):
        return None


def _format_liquidity_label() -> str:
    """把成交额门槛格式化成人话：≥1亿用「亿」，否则用「万」。"""
    v = float(MIN_QUOTE_VOLUME)
    if v >= 100_000_000:
        yi = v / 100_000_000
        return f"{int(yi)}亿" if yi == int(yi) else f"{yi:g}亿"
    wan = v / 10_000
    return f"{int(wan)}万" if wan == int(wan) else f"{wan:g}万"


def enrich_alt_rotation_advice(
    safety: dict[str, Any],
    *,
    items: list[dict[str, Any]] | None = None,
    source: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """根据大盘风控 + 雷达结果，生成山寨短线实战建议。"""
    out = dict(safety)
    change = float(out.get("btc_change_1h") or 0.0)
    vol = float(out.get("btc_volatility_1h") or 0.0)
    trend = out.get("btc_trend") or "flat"
    no_long = bool(out.get("no_long"))
    radar_items = items if items is not None else (radar_state.get("items") or [])
    src = source or radar_state.get("source") or "init"
    top_syms = [str(it.get("symbol") or "") for it in radar_items if it.get("symbol")]
    top_str = "、".join(top_syms[:RADAR_TOP_N]) if top_syms else "暂无"
    has_l1 = any(s.upper() in ALT_SEASON_BASES for s in top_syms)
    funding_ok = _btc_funding_healthy()
    liq = _format_liquidity_label()
    fee_pct = f"{MAX_ABS_FUNDING * 100:.2f}"

    # ---- 选币依据小字（与下方雷达模块呼应）----
    basis_parts: list[str] = []
    if no_long:
        basis_parts.append("全局禁多生效 · 雷达已清空山寨多头推荐")
    elif src == "futures":
        basis_parts.append(f"当前已过滤高费率币（|费率|≤{fee_pct}%）")
        basis_parts.append(f"成交额 > {liq} USDT")
    elif src == "spot_proxy":
        basis_parts.append("现货代理：合约费率暂不可用，已跳过费率过滤")
        basis_parts.append(f"成交额 > {liq} USDT")
    elif src == "demo":
        basis_parts.append("演示数据 · 筛选规则仍按成交额/相对强度模拟")
    else:
        basis_parts.append(f"筛选中 · 目标成交额 > {liq} USDT · |费率|≤{fee_pct}%")

    if top_syms and not no_long:
        basis_parts.append(f"推荐前三：{top_str}")
    if note and not no_long:
        for part in [p.strip() for p in str(note).split("·") if p.strip()]:
            joined = " · ".join(basis_parts)
            if part in joined:
                continue
            if "费率" in part and any("费率" in b for b in basis_parts):
                continue
            if "成交额" in part and any("成交额" in b for b in basis_parts):
                continue
            basis_parts.append(part)
    if funding_ok is True:
        basis_parts.append("BTC 资金费率健康")
    elif funding_ok is False and not no_long:
        basis_parts.append("BTC 资金费率偏高，开仓需更轻")

    out["advice_basis"] = " · ".join(basis_parts)

    # ---- 主建议文案 ----
    if no_long:
        out["advice_mode"] = "risk"
        out["status"] = "risk"
        out["advice_title"] = "🚨 建议山寨：大盘避险中，严禁开多山寨，建议空仓观望"
        out["label"] = out["advice_title"]
        return out

    # 山寨季传导：BTC 强势上涨 + 雷达出现 L1/SOL 联动币
    if trend == "up" and change >= 0.35 and top_syms and (has_l1 or change >= 0.8):
        out["advice_mode"] = "season"
        out["status"] = "season"
        if has_l1:
            out["advice_title"] = (
                "🚀 建议山寨：山寨季传导期，重点做多强势 L1/SOL 联动币"
            )
        else:
            out["advice_title"] = (
                "🚀 建议山寨：大盘强势带动资金外溢，优先雷达前列相对强势币"
            )
        out["label"] = out["advice_title"]
        return out

    # 震荡企稳：BTC 不暴跌、波动可控
    stable = abs(change) < 0.8 and vol < BTC_HIGH_VOL_1H
    if stable and (funding_ok is not False):
        out["advice_mode"] = "rotate"
        out["status"] = "rotate"
        if top_syms:
            out["advice_title"] = (
                "🟢 建议山寨：大盘震荡企稳，可择优轻仓埋伏雷达前三强"
            )
        else:
            out["advice_title"] = (
                "🟢 建议山寨：大盘震荡企稳，等待雷达选出相对强势标的"
            )
        out["label"] = out["advice_title"]
        return out

    # 其余：谨慎观望（软下跌 / 高波动 / 费率过热）
    out["advice_mode"] = "watch"
    out["status"] = "watch"
    if funding_ok is False:
        out["advice_title"] = (
            "⚠️ 建议山寨：费率偏热，暂缓追高，仅观察雷达强弱不追单"
        )
    elif trend == "down":
        out["advice_title"] = (
            "⚠️ 建议山寨：大盘偏弱，山寨只看不追，等待企稳信号"
        )
    else:
        out["advice_title"] = (
            "⚠️ 建议山寨：波动抬升，控制仓位，优先观望雷达强弱切换"
        )
    out["label"] = out["advice_title"]
    return out


def screen_radar_sync() -> dict[str, Any]:
    """双子星（币安+OKX）综合指标 TOP 10 狙击雷达。"""
    twin_bases, twin_meta = get_twin_star_bases()
    safety = evaluate_market_safety_sync("spot")

    tickers = _fetch_json_hosts("/fapi/v1/ticker/24hr", FUTURES_REST_HOSTS)
    premiums = _fetch_json_hosts("/fapi/v1/premiumIndex", FUTURES_REST_HOSTS)
    source = "twin_star"
    funding_map: dict[str, float] = {}
    metrics_by_base: dict[str, dict[str, Any]] = {}

    if tickers and premiums:
        for p in premiums:
            sym = p.get("symbol")
            if not sym:
                continue
            try:
                funding_map[sym] = float(p.get("lastFundingRate") or 0)
            except (TypeError, ValueError):
                continue
        for t in tickers:
            pair = t.get("symbol", "")
            if not _is_alt_usdt_pair(pair):
                continue
            base = _base_from_pair(pair)
            if twin_bases and base not in twin_bases:
                continue
            try:
                quote_vol = float(t.get("quoteVolume") or 0)
                last_price = float(t.get("lastPrice") or 0)
            except (TypeError, ValueError):
                continue
            if quote_vol < MIN_QUOTE_VOLUME or last_price <= 0:
                continue
            funding = funding_map.get(pair)
            if funding is not None and abs(funding) > MAX_ABS_FUNDING:
                continue
            metrics_by_base[base] = {
                "pair": pair,
                "symbol": base,
                "price": last_price,
                "quote_volume": quote_vol,
                "funding_rate": funding,
                "change_pct_24h": float(t.get("priceChangePercent") or 0),
                "exchanges": ["binance", "okx"],
            }
        source = "twin_star_binance"
    else:
        okx_tickers = _fetch_json_hosts(
            "/api/v5/market/tickers?instType=SWAP", OKX_REST_HOSTS
        )
        spot_tickers = _fetch_json_hosts("/api/v3/ticker/24hr", SPOT_REST_HOSTS)
        spot_map: dict[str, dict[str, Any]] = {}
        for t in spot_tickers or []:
            pair = t.get("symbol", "")
            if _is_alt_usdt_pair(pair):
                spot_map[_base_from_pair(pair)] = t

        for t in (okx_tickers or {}).get("data") or []:
            inst = t.get("instId") or ""
            parts = inst.split("-")
            if len(parts) < 3 or parts[1] != "USDT" or parts[2] != "SWAP":
                continue
            base = parts[0].upper()
            if base in EXCLUDE_BASES:
                continue
            if twin_bases and base not in twin_bases:
                continue
            if twin_meta.get("binance_source") == "binance_spot_proxy" and base not in spot_map:
                continue
            try:
                quote_vol = float(t.get("volCcy24h") or 0)
                last_price = float(t.get("last") or 0)
            except (TypeError, ValueError):
                continue
            if quote_vol < MIN_QUOTE_VOLUME or last_price <= 0:
                continue
            spot = spot_map.get(base)
            if spot:
                try:
                    last_price = float(spot.get("lastPrice") or last_price)
                    quote_vol = max(quote_vol, float(spot.get("quoteVolume") or 0))
                except (TypeError, ValueError):
                    pass
            ch24 = 0.0
            if spot:
                try:
                    ch24 = float(spot.get("priceChangePercent") or 0)
                except (TypeError, ValueError):
                    ch24 = 0.0
            metrics_by_base[base] = {
                "pair": f"{base}USDT",
                "okx_inst": inst,
                "symbol": base,
                "price": last_price,
                "quote_volume": quote_vol,
                "funding_rate": None,
                "change_pct_24h": ch24,
                "exchanges": ["binance", "okx"],
            }
        source = "twin_star_okx"

    if not metrics_by_base:
        demo = _demo_radar_result("no_twin_star_candidates", safety)
        demo["market_safety"] = enrich_alt_rotation_advice(
            safety,
            items=demo.get("items") or [],
            source=demo.get("source"),
            note=demo.get("note"),
        )
        demo["note"] = (
            (demo.get("note") or "")
            + f" · 双子星白名单∩={twin_meta.get('twin_count', 0)}"
        )
        demo["twin_meta"] = twin_meta
        return demo

    if safety["no_long"]:
        enriched = enrich_alt_rotation_advice(
            safety, items=[], source=source, note="全局禁多令生效，暂停山寨多头推荐"
        )
        return {
            "items": [],
            "btc_change_1h": safety["btc_change_1h"],
            "source": source,
            "simulated": False,
            "updated_at": utc_now_iso(),
            "candidate_count": len(metrics_by_base),
            "twin_meta": twin_meta,
            "note": "全局禁多令生效，暂停山寨多头推荐",
            "market_safety": enriched,
        }

    candidates = sorted(
        metrics_by_base.values(),
        key=lambda x: x["quote_volume"],
        reverse=True,
    )[:RADAR_SHORTLIST]

    btc_change = float(safety.get("btc_change_1h") or 0.0)
    scored: list[dict[str, Any]] = []
    for c in candidates:
        ch = _fetch_change_1h_sync(c["pair"], "spot")
        if ch is None:
            ch = _fetch_change_1h_sync(c["pair"], "futures")
        if ch is None:
            continue
        c["change_1h"] = round(ch, 4)
        c["vs_btc_1h"] = round(ch - btc_change, 4)
        if c["change_1h"] <= btc_change:
            continue
        if c.get("funding_rate") is None and c.get("okx_inst"):
            fr = _fetch_okx_funding_sync(c["okx_inst"])
            if fr is not None and abs(fr) > MAX_ABS_FUNDING:
                continue
            c["funding_rate"] = fr
        c["composite_score"] = _composite_sniper_score(c)
        scored.append(c)

    scored.sort(key=lambda x: x.get("composite_score", 0), reverse=True)
    top = scored[:RADAR_TOP_N]

    def _build_row(rank_c: tuple[int, dict[str, Any]]) -> dict[str, Any]:
        i, c = rank_c
        row = {
            "rank": i,
            "symbol": c["symbol"],
            "pair": c["pair"],
            "price": c["price"],
            "change_1h": c.get("change_1h"),
            "vs_btc_1h": c.get("vs_btc_1h"),
            "funding_rate": c.get("funding_rate"),
            "quote_volume": c["quote_volume"],
            "change_pct_24h": c.get("change_pct_24h"),
            "composite_score": c.get("composite_score"),
            "exchanges": c.get("exchanges") or ["binance", "okx"],
        }
        return _attach_decision(row, safety, market="spot")

    items: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=min(6, max(1, len(top)))) as pool:
        futs = {pool.submit(_build_row, (i, c)): i for i, c in enumerate(top, 1)}
        built: dict[int, dict[str, Any]] = {}
        for fut in as_completed(futs):
            try:
                row = fut.result()
                built[int(row["rank"])] = row
            except Exception as exc:
                logger.warning("TOP10 row build failed: %s", exc)
        items = [built[i] for i in sorted(built.keys())]

    ranked_pack = _apply_ambush_ranking(items)
    items = ranked_pack["items"]
    top3 = ranked_pack["top3"]

    note_bits = [
        f"双子星白名单∩{twin_meta.get('twin_count', 0)}",
        f"币安源 {twin_meta.get('binance_source')}",
        f"候选 {len(metrics_by_base)} → 强于BTC {len(scored)} → TOP {len(items)}",
        f"硬过滤通过 {ranked_pack.get('passed_count', 0)} · 推荐前三 "
        + ("、".join(x.get("symbol") or "" for x in top3) or "暂无"),
    ]
    if ranked_pack.get("top3_fallback"):
        note_bits.append("前三含降级备选")
    if twin_meta.get("binance_source") == "binance_spot_proxy":
        note_bits.append("币安合约不可达，白名单用现货代理∩OKX永续")
    long_n = sum(1 for it in items if it.get("suggest_direction") == "long_light")
    note_bits.append(f"决策：{long_n}/{len(items)} 轻仓做多")

    advice_items = top3 if top3 else items
    return {
        "items": items,
        "top3": top3,
        "top3_fallback": ranked_pack.get("top3_fallback"),
        "btc_change_1h": round(btc_change, 4),
        "source": source,
        "simulated": False,
        "updated_at": utc_now_iso(),
        "candidate_count": len(metrics_by_base),
        "twin_meta": twin_meta,
        "note": " · ".join(note_bits),
        "market_safety": enrich_alt_rotation_advice(
            safety, items=advice_items, source=source, note=" · ".join(note_bits)
        ),
    }


def _demo_radar_result(reason: str, safety: dict[str, Any] | None = None) -> dict[str, Any]:
    """完全不可达时的演示雷达列表（费率均 < 0.03%）。"""
    if safety is None:
        safety = {
            "status": "safe",
            "no_long": False,
            "label": "安全可做多",
            "btc_change_1h": -0.2,
            "btc_volatility_1h": 0.6,
            "btc_trend": "flat",
            "reason": "演示风控数据",
            "updated_at": utc_now_iso(),
        }
    if safety.get("no_long"):
        note = f"演示 · 全局禁多令（{reason}）"
        return {
            "items": [],
            "btc_change_1h": safety.get("btc_change_1h"),
            "source": "demo",
            "simulated": True,
            "updated_at": utc_now_iso(),
            "candidate_count": 0,
            "note": note,
            "market_safety": enrich_alt_rotation_advice(
                safety, items=[], source="demo", note=note
            ),
        }

    btc_ch = float(safety.get("btc_change_1h") or -0.2)
    demo = [
        ("SOL", 148.2, 1.4, 0.00012, 180_000_000),
        ("XRP", 0.62, 1.1, -0.00005, 150_000_000),
        ("DOGE", 0.162, 0.9, 0.00008, 120_000_000),
        ("SUI", 2.15, 1.2, 0.0001, 95_000_000),
        ("NEAR", 3.4, 0.95, -0.0001, 88_000_000),
        ("AVAX", 22.1, 0.85, 0.00005, 80_000_000),
        ("LINK", 14.2, 0.75, 0.00002, 72_000_000),
        ("APT", 6.1, 0.7, -0.00008, 65_000_000),
        ("OP", 1.05, 0.65, 0.00015, 58_000_000),
        ("ARB", 0.52, 0.55, 0.00009, 55_000_000),
    ]
    items = []
    for i, (sym, price, ch, fr, vol) in enumerate(demo, 1):
        price *= 1 + random.uniform(-0.002, 0.002)
        ch = max(ch + random.uniform(-0.1, 0.1), btc_ch + 0.05)
        row = {
            "rank": i,
            "symbol": sym,
            "pair": f"{sym}USDT",
            "price": round(price, 6 if price < 1 else 4),
            "change_1h": round(ch, 4),
            "vs_btc_1h": round(ch - btc_ch, 4),
            "funding_rate": fr,
            "quote_volume": vol,
            "change_pct_24h": round(ch * 3, 2),
            "composite_score": round(70 - i * 3 + random.uniform(0, 2), 1),
            "exchanges": ["binance", "okx"],
        }
        items.append(_attach_decision(row, safety, market="spot", simulated=True))
    ranked_pack = _apply_ambush_ranking(items)
    items = ranked_pack["items"]
    top3 = ranked_pack["top3"]
    note = f"演示数据（{reason}）· 双子星 TOP10"
    if ranked_pack.get("top3_fallback"):
        note += " · 前三含降级备选"
    return {
        "items": items,
        "top3": top3,
        "top3_fallback": ranked_pack.get("top3_fallback"),
        "btc_change_1h": btc_ch,
        "source": "demo",
        "simulated": True,
        "updated_at": utc_now_iso(),
        "candidate_count": 0,
        "note": note,
        "market_safety": enrich_alt_rotation_advice(
            safety, items=top3 or items, source="demo", note=note
        ),
    }


MARKET_SAFETY_KEYS = (
    "status",
    "no_long",
    "label",
    "advice_mode",
    "advice_title",
    "advice_basis",
    "btc_change_1h",
    "btc_volatility_1h",
    "btc_trend",
    "reason",
    "updated_at",
)


async def publish_market_safety(safety: dict[str, Any]) -> None:
    market_safety.update(safety)
    await manager.broadcast(
        {
            "type": "market_safety",
            **{k: market_safety[k] for k in MARKET_SAFETY_KEYS},
            "ts": utc_now_iso(),
        }
    )


async def publish_radar(result: dict[str, Any]) -> None:
    """更新雷达状态并广播；触发动态行情订阅刷新。"""
    safety = result.get("market_safety")
    if safety:
        await publish_market_safety(safety)

    radar_state["items"] = result.get("items") or []
    radar_state["top3"] = result.get("top3") or []
    radar_state["top3_fallback"] = bool(result.get("top3_fallback"))
    radar_state["btc_change_1h"] = result.get("btc_change_1h")
    radar_state["source"] = result.get("source")
    radar_state["simulated"] = bool(result.get("simulated"))
    radar_state["updated_at"] = result.get("updated_at") or utc_now_iso()

    for it in radar_state["items"]:
        ensure_price_slot(it["symbol"])
        if latest_prices[it["symbol"]]["price"] is None:
            latest_prices[it["symbol"]]["price"] = it["price"]
            latest_prices[it["symbol"]]["change_pct_24h"] = it.get("change_pct_24h")

    # 极速狙击：只对雷达 #1 建仓，并传入实时价做市价快成交
    if alt_sim_bot is not None and radar_state["top3"]:
        try:
            live = {
                it["symbol"]: float(latest_prices[it["symbol"]]["price"])
                for it in radar_state["top3"]
                if it.get("symbol") in latest_prices
                and latest_prices[it["symbol"]].get("price")
            }
            # 补上 top3 自带的快照价
            for it in radar_state["top3"]:
                sym = it.get("symbol")
                if sym and sym not in live and it.get("price"):
                    live[sym] = float(it["price"])
            events = alt_sim_bot.on_radar_top3(radar_state["top3"], live_prices=live)
            if events:
                await manager.broadcast(alt_sim_bot.snapshot())
        except Exception as exc:  # pragma: no cover
            logger.exception("alt_sim on_radar_top3 error: %s", exc)

    await manager.broadcast(
        {
            "type": "radar",
            "items": radar_state["items"],
            "top3": radar_state["top3"],
            "top3_fallback": radar_state["top3_fallback"],
            "btc_change_1h": radar_state["btc_change_1h"],
            "source": radar_state["source"],
            "simulated": radar_state["simulated"],
            "note": result.get("note") or "",
            "no_long": market_safety.get("no_long", False),
            "updated_at": radar_state["updated_at"],
            "ts": utc_now_iso(),
        }
    )
    _get_radar_event().set()
    logger.info(
        "Radar updated source=%s items=%s btc_1h=%s no_long=%s",
        radar_state["source"],
        [i["symbol"] for i in radar_state["items"]],
        radar_state["btc_change_1h"],
        market_safety.get("no_long"),
    )


async def radar_screener_loop() -> None:
    """每分钟跑一遍稳健选币 + 大盘风控。"""
    await asyncio.sleep(2)
    while True:
        try:
            result = await asyncio.to_thread(screen_radar_sync)
            await publish_radar(result)
            safety = result.get("market_safety") or market_safety
            detail = (
                f"轮动雷达（{result.get('source')}）: "
                + (", ".join(i["symbol"] for i in result.get("items") or []) or "空")
                + f" · {safety.get('label')}"
            )
            await manager.broadcast(
                build_system_event(
                    "radar_updated" if not safety.get("no_long") else "no_long_active",
                    detail,
                )
            )
        except Exception as exc:
            logger.exception("Radar screener error: %s", exc)
            await manager.broadcast(
                build_system_event("radar_error", f"稳健轮动筛选失败: {exc}")
            )
        await asyncio.sleep(RADAR_INTERVAL_SEC)


async def alt_sim_loop() -> None:
    """#1 狙击模拟仓：用最新价推进补仓/止盈止损，并实时推送权益。"""
    if alt_sim_bot is None:
        return
    await asyncio.sleep(2)
    last_broadcast = 0.0
    while True:
        try:
            symbols = {p["symbol"] for p in alt_sim_bot.open_positions()}
            # 也盯住雷达 #1，便于无持仓时用实时价秒开（由 publish_radar 主路径触发）
            for it in (radar_state.get("top3") or [])[:1]:
                if it.get("symbol"):
                    symbols.add(it["symbol"])
            prices = {
                s: latest_prices[s]["price"]
                for s in symbols
                if s in latest_prices and latest_prices[s].get("price")
            }
            events = alt_sim_bot.on_prices(prices) if prices else []
            now = time.time()
            if events or (now - last_broadcast) >= 8:
                await manager.broadcast(alt_sim_bot.snapshot())
                last_broadcast = now
            for ev in events:
                await manager.broadcast(
                    build_system_event(
                        "alt_sim_trade",
                        f"狙击仓 {ev.get('symbol')} {ev.get('action_label')}"
                        + (
                            f" · PnL {ev.get('pnl_usd'):+.2f}U"
                            if ev.get("pnl_usd") is not None
                            else ""
                        ),
                    )
                )
        except Exception as exc:  # pragma: no cover
            logger.exception("alt_sim loop error: %s", exc)
        await asyncio.sleep(2)


async def audit_watchdog_loop() -> None:
    """每小时自动对账；平仓路径也会即时触发，此处做兜底巡检。"""
    import audit_ledger as AL

    await asyncio.sleep(30)
    while True:
        try:
            if alt_sim_bot is not None:
                result = alt_sim_bot.run_audit(auto_correct=True)
                if not result.get("ok"):
                    await manager.broadcast(
                        build_system_event("audit_error", result.get("alert") or "CEX 账目对账失败")
                    )
                await manager.broadcast(alt_sim_bot.snapshot())
            if pump_bot is not None:
                result = pump_bot.broker.run_audit(auto_correct=True)
                if not result.get("ok"):
                    await manager.broadcast(
                        build_system_event("audit_error", result.get("alert") or "Pump 账目对账失败")
                    )
                await manager.broadcast(pump_bot.snapshot())
        except Exception as exc:  # pragma: no cover
            logger.exception("audit watchdog error: %s", exc)
        await asyncio.sleep(AL.AUDIT_INTERVAL_SEC)


def _radar_stream_symbols() -> list[str]:
    """需要动态订阅 aggTrade 的山寨列表（雷达 + 前端 watch）。"""
    syms: set[str] = set(radar_state.get("watch_symbols") or set())
    for it in radar_state.get("items") or []:
        syms.add(it["symbol"])
    # 不重复订阅 BTC/ETH（主连接已有）
    return sorted(s for s in syms if s not in ("BTC", "ETH"))


async def radar_trade_listener() -> None:
    """为雷达/watch 山寨维护独立的现货 aggTrade 组合流，列表变化时自动重连。"""
    backoff = 1
    while True:
        symbols = _radar_stream_symbols()
        if not symbols:
            ev = _get_radar_event()
            ev.clear()
            try:
                await asyncio.wait_for(ev.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
            continue

        streams = "/".join(f"{s.lower()}usdt@aggTrade" for s in symbols)
        urls = [
            f"wss://data-stream.binance.vision/stream?streams={streams}",
            f"wss://stream.binance.com:9443/stream?streams={streams}",
            f"wss://stream.binance.com:443/stream?streams={streams}",
        ]
        connected = False
        for ws_url in urls:
            # 若中途雷达列表变化，打断当前连接
            ev = _get_radar_event()
            ev.clear()
            try:
                logger.info("Radar trade stream: %s", ",".join(symbols))
                async with websockets.connect(
                    ws_url,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    open_timeout=20,
                    max_size=2**22,
                ) as ws:
                    connected = True
                    backoff = 1
                    await manager.broadcast(
                        build_system_event(
                            "radar_stream_connected",
                            f"山寨实时流已订阅: {', '.join(symbols)}",
                        )
                    )
                    while True:
                        recv_task = asyncio.create_task(ws.recv())
                        wait_task = asyncio.create_task(ev.wait())
                        done, pending = await asyncio.wait(
                            {recv_task, wait_task},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        for t in pending:
                            t.cancel()
                        if wait_task in done:
                            # 列表变化 → 重连
                            break
                        if recv_task in done:
                            try:
                                message = recv_task.result()
                            except Exception:
                                break
                            await handle_binance_message(message)
                    break  # 跳出 urls 循环，用新 symbols 重建
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Radar trade stream error (%s): %s", ws_url.split("?")[0], exc)
                continue
        if not connected:
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
        else:
            # 列表变化触发的重连，短暂等待即可
            await asyncio.sleep(0.3)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_system_event(event: str, detail: str = "") -> dict[str, Any]:
    return {
        "type": "system",
        "event": event,
        "detail": detail,
        "exchange_connected": exchange_status["connected"],
        "ts": utc_now_iso(),
    }


async def handle_binance_message(raw: str) -> None:
    """解析币安推送并更新缓存 / 广播。"""
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Invalid JSON from Binance: %s", raw[:200])
        return

    data = envelope.get("data") or envelope
    event_type = data.get("e")

    if event_type == "aggTrade":
        pair = data.get("s", "")
        symbol = SYMBOL_MAP.get(pair) or (
            _base_from_pair(pair) if pair.endswith("USDT") else None
        )
        if not symbol:
            return
        ensure_price_slot(symbol)
        price = float(data["p"])
        ts = datetime.fromtimestamp(data["T"] / 1000, tz=timezone.utc).isoformat()
        latest_prices[symbol]["price"] = price
        latest_prices[symbol]["ts"] = ts
        if symbol in AGG_SYMBOLS:
            exchange_modes["binance"] = "ws"
            update_quote("binance", symbol, price, "ws")
        await manager.broadcast(
            {
                "type": "trade",
                "symbol": symbol,
                "price": price,
                "qty": float(data.get("q", 0)),
                "trade_id": data.get("a"),
                "ts": ts,
            }
        )
        return

    if event_type == "24hrTicker":
        pair = data.get("s", "")
        symbol = SYMBOL_MAP.get(pair) or (
            _base_from_pair(pair) if pair.endswith("USDT") else None
        )
        if not symbol:
            return
        ensure_price_slot(symbol)
        price = float(data["c"])
        high_24h = float(data["h"])
        low_24h = float(data["l"])
        change_24h = float(data["p"])
        change_pct_24h = float(data["P"])
        volume_24h = float(data["v"])
        ts = datetime.fromtimestamp(data["E"] / 1000, tz=timezone.utc).isoformat()

        latest_prices[symbol].update(
            {
                "price": price,
                "high_24h": high_24h,
                "low_24h": low_24h,
                "change_24h": change_24h,
                "change_pct_24h": change_pct_24h,
                "volume_24h": volume_24h,
                "ts": ts,
            }
        )
        await manager.broadcast(
            {
                "type": "ticker",
                "symbol": symbol,
                "price": price,
                "high_24h": high_24h,
                "low_24h": low_24h,
                "change_24h": change_24h,
                "change_pct_24h": change_pct_24h,
                "volume_24h": volume_24h,
                "ts": ts,
            }
        )


async def binance_listener() -> None:
    """持续连接币安，断线后指数退避重连，并轮询多个可用端点。"""
    backoff = 1
    max_backoff = 60
    url_index = 0

    while True:
        exchange_status["reconnect_attempt"] += 1
        attempt = exchange_status["reconnect_attempt"]
        ws_url = BINANCE_WS_URLS[url_index % len(BINANCE_WS_URLS)]
        await manager.broadcast(
            build_system_event(
                "exchange_reconnecting",
                f"正在连接币安 WebSocket（第 {attempt} 次）…",
            )
        )
        logger.info("Connecting to Binance (attempt %d): %s", attempt, ws_url.split("?")[0])

        try:
            async with websockets.connect(
                ws_url,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=5,
                open_timeout=30,
                max_size=2**22,
            ) as ws:
                exchange_status["connected"] = True
                exchange_status["last_error"] = None
                exchange_status["reconnect_attempt"] = 0
                backoff = 1
                await manager.broadcast(
                    build_system_event("exchange_connected", "已连接币安实时行情流")
                )
                logger.info("Connected to Binance WebSocket")

                async for message in ws:
                    await handle_binance_message(message)

        except asyncio.CancelledError:
            exchange_status["connected"] = False
            raise
        except Exception as exc:
            exchange_status["connected"] = False
            exchange_status["last_error"] = str(exc)
            url_index += 1
            logger.error("Binance connection error: %s", exc)
            await manager.broadcast(
                build_system_event(
                    "exchange_disconnected",
                    f"币安连接断开: {exc}；{backoff}s 后重连",
                )
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)


async def handle_futures_message(raw: str) -> None:
    """解析币安合约推送：markPriceUpdate（资金费率） / forceOrder（爆仓）。"""
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError:
        return

    data = envelope.get("data") or envelope
    event_type = data.get("e")

    if event_type == "markPriceUpdate":
        symbol = SYMBOL_MAP.get(data.get("s", ""))
        if not symbol:
            return
        funding_rate = float(data.get("r", 0) or 0)
        mark_price = float(data.get("p", 0) or 0)
        next_ts = data.get("T")
        next_iso = (
            datetime.fromtimestamp(next_ts / 1000, tz=timezone.utc).isoformat()
            if next_ts
            else None
        )
        ts = datetime.fromtimestamp(data["E"] / 1000, tz=timezone.utc).isoformat()
        futures_data[symbol].update(
            {
                "funding_rate": funding_rate,
                "mark_price": mark_price,
                "next_funding_ts": next_iso,
                "simulated": False,
                "ts": ts,
            }
        )
        if symbol in AGG_SYMBOLS and mark_price:
            exchange_modes["binance"] = "ws"
            update_quote("binance", symbol, mark_price, "ws")
        await manager.broadcast(
            {
                "type": "funding",
                "symbol": symbol,
                "funding_rate": funding_rate,
                "mark_price": mark_price,
                "next_funding_ts": next_iso,
                "simulated": False,
                "ts": ts,
            }
        )
        return

    if event_type == "forceOrder":
        order = data.get("o", {})
        symbol = SYMBOL_MAP.get(order.get("s", ""))
        if not symbol:
            return
        side = order.get("S", "")  # SELL = 多头被强平, BUY = 空头被强平
        qty = float(order.get("q", 0) or 0)
        price = float(order.get("ap") or order.get("p") or 0)
        notional = qty * price
        ts = datetime.fromtimestamp(
            (order.get("T") or data.get("E")) / 1000, tz=timezone.utc
        ).isoformat()
        await manager.broadcast(
            {
                "type": "liquidation",
                "symbol": symbol,
                "side": side,
                "qty": qty,
                "price": price,
                "notional": notional,
                "simulated": False,
                "ts": ts,
            }
        )


async def futures_listener() -> None:
    """连接币安合约 WS（资金费率 + 爆仓流），失败则退避重连并轮询端点。"""
    backoff = 2
    max_backoff = 60
    url_index = 0

    while True:
        ws_url = BINANCE_FUTURES_WS_URLS[url_index % len(BINANCE_FUTURES_WS_URLS)]
        logger.info("Connecting to Binance Futures: %s", ws_url.split("?")[0])
        try:
            async with websockets.connect(
                ws_url,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=5,
                open_timeout=15,
                max_size=2**22,
            ) as ws:
                futures_status["ws_connected"] = True
                futures_status["demo_active"] = False
                futures_status["last_error"] = None
                backoff = 2
                await manager.broadcast(
                    build_system_event("futures_connected", "已连接币安合约数据流（资金费率 / 爆仓）")
                )
                logger.info("Connected to Binance Futures WebSocket")
                async for message in ws:
                    await handle_futures_message(message)
        except asyncio.CancelledError:
            futures_status["ws_connected"] = False
            raise
        except Exception as exc:
            futures_status["ws_connected"] = False
            futures_status["last_error"] = str(exc)
            url_index += 1
            logger.warning("Binance Futures error: %s", exc)
            await manager.broadcast(
                build_system_event(
                    "futures_disconnected",
                    f"合约数据流断开: {exc}；{backoff}s 后重连",
                )
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)


def _fetch_ratio_sync(symbol_pair: str) -> dict[str, Any] | None:
    """同步拉取多空比（在线程池中调用）。"""
    for host in BINANCE_RATIO_HOSTS:
        url = (
            f"{host}/futures/data/globalLongShortAccountRatio"
            f"?symbol={symbol_pair}&period=5m&limit=1"
        )
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "crypto-monitor/1.0"})
            with urllib.request.urlopen(req, timeout=12) as resp:
                arr = json.loads(resp.read().decode())
                if arr:
                    return arr[-1]
        except Exception:
            continue
    return None


async def ratio_poller() -> None:
    """周期性轮询 BTC/ETH 多空持仓人数比（REST）。"""
    pair = {"BTC": "BTCUSDT", "ETH": "ETHUSDT"}
    while True:
        got_any = False
        for sym, sp in pair.items():
            try:
                row = await asyncio.to_thread(_fetch_ratio_sync, sp)
            except Exception as exc:
                logger.warning("ratio fetch error: %s", exc)
                row = None
            if row:
                got_any = True
                ratio = float(row.get("longShortRatio", 0) or 0)
                long_acc = float(row.get("longAccount", 0) or 0)
                short_acc = float(row.get("shortAccount", 0) or 0)
                futures_data[sym].update(
                    {
                        "long_short_ratio": ratio,
                        "long_account": long_acc,
                        "short_account": short_acc,
                        "ts": utc_now_iso(),
                    }
                )
                await manager.broadcast(
                    {
                        "type": "ratio",
                        "symbol": sym,
                        "long_short_ratio": ratio,
                        "long_account": long_acc,
                        "short_account": short_acc,
                        "simulated": False,
                        "ts": utc_now_iso(),
                    }
                )
        futures_status["ratio_ok"] = got_any
        await asyncio.sleep(30 if got_any else 60)


async def demo_futures_generator() -> None:
    """合约接口不可达时输出「模拟」资金费率/多空比/爆仓，保证面板不空白。

    - DEMO_FUTURES=off：从不产生模拟数据
    - DEMO_FUTURES=on：始终产生模拟数据
    - DEMO_FUTURES=auto（默认）：仅当真实合约流未连接时产生
    """
    if DEMO_FUTURES == "off":
        return

    # 模拟资金费率基线（随机游走）
    base_funding = {"BTC": 0.0001, "ETH": 0.00005}
    base_ratio = {"BTC": 1.8, "ETH": 1.5}
    tick = 0

    while True:
        await asyncio.sleep(3)
        real_active = futures_status["ws_connected"] or futures_status["ratio_ok"]
        should_demo = DEMO_FUTURES == "on" or (DEMO_FUTURES == "auto" and not real_active)
        futures_status["demo_active"] = should_demo
        if not should_demo:
            continue

        tick += 1
        now_ms = datetime.now(timezone.utc).timestamp() * 1000
        # 每 8 小时结算一次：模拟一个 UTC 对齐的下次结算时间
        next_settle_ms = (int(now_ms // (8 * 3600_000)) + 1) * (8 * 3600_000)
        next_iso = datetime.fromtimestamp(next_settle_ms / 1000, tz=timezone.utc).isoformat()

        for sym in ("BTC", "ETH"):
            base_funding[sym] += random.uniform(-0.00002, 0.00002)
            base_funding[sym] = max(-0.003, min(0.003, base_funding[sym]))
            base_ratio[sym] += random.uniform(-0.05, 0.05)
            base_ratio[sym] = max(0.4, min(3.5, base_ratio[sym]))

            mark = latest_prices[sym]["price"]
            futures_data[sym].update(
                {
                    "funding_rate": round(base_funding[sym], 6),
                    "mark_price": mark,
                    "next_funding_ts": next_iso,
                    "long_short_ratio": round(base_ratio[sym], 2),
                    "long_account": round(base_ratio[sym] / (1 + base_ratio[sym]), 4),
                    "short_account": round(1 / (1 + base_ratio[sym]), 4),
                    "simulated": True,
                    "ts": utc_now_iso(),
                }
            )
            await manager.broadcast(
                {
                    "type": "funding",
                    "symbol": sym,
                    "funding_rate": round(base_funding[sym], 6),
                    "mark_price": mark,
                    "next_funding_ts": next_iso,
                    "simulated": True,
                    "ts": utc_now_iso(),
                }
            )
            await manager.broadcast(
                {
                    "type": "ratio",
                    "symbol": sym,
                    "long_short_ratio": round(base_ratio[sym], 2),
                    "long_account": round(base_ratio[sym] / (1 + base_ratio[sym]), 4),
                    "short_account": round(1 / (1 + base_ratio[sym]), 4),
                    "simulated": True,
                    "ts": utc_now_iso(),
                }
            )

        # 偶发模拟爆仓事件
        if random.random() < 0.35:
            sym = random.choice(["BTC", "ETH"])
            price = latest_prices[sym]["price"]
            if price:
                side = random.choice(["SELL", "BUY"])
                qty = round(random.uniform(0.05, 5.0), 3)
                notional = qty * price
                await manager.broadcast(
                    {
                        "type": "liquidation",
                        "symbol": sym,
                        "side": side,
                        "qty": qty,
                        "price": price,
                        "notional": notional,
                        "simulated": True,
                        "ts": utc_now_iso(),
                    }
                )


@app.on_event("startup")
async def on_startup() -> None:
    _get_radar_event()
    app.state.binance_task = asyncio.create_task(binance_listener())
    app.state.futures_task = asyncio.create_task(futures_listener())
    app.state.ratio_task = asyncio.create_task(ratio_poller())
    app.state.demo_task = asyncio.create_task(demo_futures_generator())
    app.state.radar_task = asyncio.create_task(radar_screener_loop())
    app.state.radar_trade_task = asyncio.create_task(radar_trade_listener())
    app.state.okx_task = asyncio.create_task(okx_ws_loop())
    app.state.bybit_task = asyncio.create_task(bybit_ws_loop())
    app.state.agg_rest_task = asyncio.create_task(exchange_rest_fallback_loop())
    app.state.agg_demo_task = asyncio.create_task(exchange_demo_loop())
    app.state.aggregator_task = asyncio.create_task(aggregator_loop())
    if pump_bot is not None:
        async def _pump_broadcast(snap: dict[str, Any]) -> None:
            await manager.broadcast(snap)

        app.state.pump_task = pump_bot.start(_pump_broadcast)
    if alt_sim_bot is not None:
        app.state.alt_sim_task = asyncio.create_task(alt_sim_loop())
    app.state.audit_task = asyncio.create_task(audit_watchdog_loop())
    logger.info(
        "Listeners started (spot + futures + ratio + demo + radar + multi-exchange + pump)"
    )


@app.on_event("shutdown")
async def on_shutdown() -> None:
    if pump_bot is not None:
        await pump_bot.stop()
    for name in (
        "binance_task",
        "futures_task",
        "ratio_task",
        "demo_task",
        "radar_task",
        "radar_trade_task",
        "okx_task",
        "bybit_task",
        "agg_rest_task",
        "agg_demo_task",
        "aggregator_task",
        "pump_task",
        "alt_sim_task",
        "audit_task",
    ):
        task = getattr(app.state, name, None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    logger.info("Shutdown complete")


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "exchange_connected": exchange_status["connected"],
        "futures": futures_status,
        "radar": {
            "source": radar_state["source"],
            "simulated": radar_state["simulated"],
            "updated_at": radar_state["updated_at"],
            "btc_change_1h": radar_state["btc_change_1h"],
            "items": radar_state["items"],
            "top3": radar_state.get("top3") or [],
            "top3_fallback": bool(radar_state.get("top3_fallback")),
        },
        "pump_bot": pump_bot.snapshot() if pump_bot is not None else {"status": "unavailable"},
        "market_safety": market_safety,
        "multi_exchange": compute_aggregate(),
        "exchange_modes": exchange_modes,
        "clients": len(manager.active),
        "latest": latest_prices,
        "futures_data": futures_data,
        "ts": utc_now_iso(),
    }


@app.get("/api/pump/status")
async def pump_status() -> dict[str, Any]:
    if pump_bot is None:
        raise HTTPException(status_code=503, detail="pumpfun 模块未加载")
    return pump_bot.snapshot()


@app.get("/api/pump/stats")
async def pump_stats_24h() -> dict[str, Any]:
    if pump_bot is None:
        raise HTTPException(status_code=503, detail="pumpfun 模块未加载")
    from pumpfun import journal as pump_journal
    from pumpfun import config as pump_cfg

    return pump_journal.compute_stats_24h(pump_cfg.BANKROLL_SOL)


@app.get("/api/pump/trades")
async def pump_trades(hours: float = 24.0, limit: int = 100) -> dict[str, Any]:
    if pump_bot is None:
        raise HTTPException(status_code=503, detail="pumpfun 模块未加载")
    from pumpfun import journal as pump_journal
    from pumpfun import config as pump_cfg

    trades = pump_journal.load_trades(hours=hours, limit=limit)
    return {
        "trades": trades,
        "stats_24h": pump_journal.compute_stats_24h(pump_cfg.BANKROLL_SOL),
        "count": len(trades),
    }


@app.get("/api/pump/trades.csv")
async def pump_trades_csv(hours: float = 24.0):
    if pump_bot is None:
        raise HTTPException(status_code=503, detail="pumpfun 模块未加载")
    from fastapi.responses import Response
    from pumpfun import journal as pump_journal

    csv_text = pump_journal.trades_to_csv(hours=hours)
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="pump_trades_24h.csv"'},
    )


@app.post("/api/pump/trades/clear")
async def pump_trades_clear() -> dict[str, Any]:
    if pump_bot is None:
        raise HTTPException(status_code=503, detail="pumpfun 模块未加载")
    from pumpfun import journal as pump_journal
    from audit_ledger import pump_ledger

    result = pump_journal.clear_trades()
    pump_ledger.clear()
    # 重置账户累计器
    br = pump_bot.broker
    locked = 0.0
    for pos in br.positions.values():
        q = float(pos["qty"]) or 1.0
        locked += float(pos["sol_spent"]) * (float(pos["qty_left"]) / q)
    br.gross_realized = 0.0
    br.total_fees = 0.0
    br.total_slippage = 0.0
    br.total_gas = 0.0
    br.realized_pnl = 0.0
    br.cash = br.bankroll - locked
    br._persist_account()
    snap = pump_bot.snapshot()
    await manager.broadcast(snap)
    return {**result, **{k: snap.get(k) for k in ("stats_24h", "trade_log", "type", "ts", "equity_sol", "realized_pnl_sol")}}


@app.get("/api/altsim/status")
async def alt_sim_status() -> dict[str, Any]:
    if alt_sim_bot is None:
        raise HTTPException(status_code=503, detail="alt_sim 模块未加载")
    return alt_sim_bot.snapshot()


@app.get("/api/altsim/trades")
async def alt_sim_trades(hours: float = 24.0, limit: int = 100) -> dict[str, Any]:
    if alt_sim_bot is None:
        raise HTTPException(status_code=503, detail="alt_sim 模块未加载")
    trades = alt_sim_bot.load_trades(hours=hours, limit=limit)
    return {
        "trades": trades,
        "stats_24h": alt_sim_bot.stats_24h(),
        "count": len(trades),
    }


@app.get("/api/altsim/trades.csv")
async def alt_sim_trades_csv(hours: float = 24.0):
    if alt_sim_bot is None:
        raise HTTPException(status_code=503, detail="alt_sim 模块未加载")
    from fastapi.responses import Response

    return Response(
        content=alt_sim_bot.trades_to_csv(hours=hours),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="alt_sim_trades_24h.csv"'},
    )


@app.post("/api/altsim/trades/clear")
async def alt_sim_trades_clear() -> dict[str, Any]:
    if alt_sim_bot is None:
        raise HTTPException(status_code=503, detail="alt_sim 模块未加载")
    snap = alt_sim_bot.clear_trades()
    await manager.broadcast(snap)
    return snap


@app.get("/api/altsim/audit")
async def alt_sim_audit() -> dict[str, Any]:
    if alt_sim_bot is None:
        raise HTTPException(status_code=503, detail="alt_sim 模块未加载")
    return alt_sim_bot.run_audit(auto_correct=True)


@app.get("/api/altsim/audit/report")
async def alt_sim_audit_report() -> dict[str, Any]:
    if alt_sim_bot is None:
        raise HTTPException(status_code=503, detail="alt_sim 模块未加载")
    return alt_sim_bot.audit_report_24h()


@app.get("/api/altsim/audit/report.csv")
async def alt_sim_audit_report_csv():
    if alt_sim_bot is None:
        raise HTTPException(status_code=503, detail="alt_sim 模块未加载")
    from fastapi.responses import Response
    import audit_ledger as AL

    report = alt_sim_bot.audit_report_24h()
    rows = AL.cex_ledger.load(hours=24.0)
    return Response(
        content=AL.report_to_csv(report, rows),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="cex_audit_24h.csv"'},
    )


@app.get("/api/pump/audit")
async def pump_audit() -> dict[str, Any]:
    if pump_bot is None:
        raise HTTPException(status_code=503, detail="pumpfun 模块未加载")
    return pump_bot.broker.run_audit(auto_correct=True)


@app.get("/api/pump/audit/report")
async def pump_audit_report() -> dict[str, Any]:
    if pump_bot is None:
        raise HTTPException(status_code=503, detail="pumpfun 模块未加载")
    stats = pump_bot.snapshot().get("stats_24h") or {}
    report = pump_bot.broker.audit_report_24h()
    report["win_rate"] = stats.get("win_rate")
    report["total_trades"] = stats.get("total_trades") or stats.get("exit_count")
    return report


@app.get("/api/pump/audit/report.csv")
async def pump_audit_report_csv():
    if pump_bot is None:
        raise HTTPException(status_code=503, detail="pumpfun 模块未加载")
    from fastapi.responses import Response
    import audit_ledger as AL

    report = pump_bot.broker.audit_report_24h()
    rows = AL.pump_ledger.load(hours=24.0)
    return Response(
        content=AL.report_to_csv(report, rows),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="pump_audit_24h.csv"'},
    )


@app.post("/api/pump/dry-run")
async def pump_set_dry_run(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    if pump_bot is None:
        raise HTTPException(status_code=503, detail="pumpfun 模块未加载")
    body = payload or {}
    dry = bool(body.get("dry_run", True))
    pump_bot.set_dry_run(dry)
    snap = pump_bot.snapshot()
    await manager.broadcast(snap)
    return snap


@app.post("/api/pump/stop")
async def pump_set_stop(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """紧急停止：生成/清除 STOP.txt。"""
    if pump_bot is None:
        raise HTTPException(status_code=503, detail="pumpfun 模块未加载")
    body = payload or {}
    active = bool(body.get("active", True))
    pump_bot.set_stop(active)
    snap = pump_bot.snapshot()
    await manager.broadcast(snap)
    return snap


# 前端 K 线图支持的周期（Binance 现货/合约 interval）
KLINE_INTERVALS = {"1m", "15m", "1h", "4h", "1d"}
KLINE_LIMIT_DEFAULT = {
    "1m": 240,
    "15m": 200,
    "1h": 168,
    "4h": 120,
    "1d": 120,
}


def _normalize_kline_symbol(symbol: str) -> str:
    s = (symbol or "BTC").upper().replace("/", "").replace("-", "")
    if s.endswith("USDT"):
        return s
    return f"{s}USDT"


def _parse_binance_klines(raw: list) -> list[dict[str, Any]]:
    candles: list[dict[str, Any]] = []
    for row in raw or []:
        try:
            candles.append(
                {
                    "t": int(row[0]),
                    "o": float(row[1]),
                    "h": float(row[2]),
                    "l": float(row[3]),
                    "c": float(row[4]),
                    "v": float(row[5]),
                }
            )
        except (TypeError, ValueError, IndexError):
            continue
    return candles


@app.get("/api/klines")
async def api_klines(
    symbol: str = "BTC",
    interval: str = "1m",
    limit: int | None = None,
) -> dict[str, Any]:
    """返回指定交易对 / 周期的 OHLCV，供前端 K 线图使用。"""
    interval = (interval or "1m").lower()
    if interval == "1d":
        interval = "1d"
    if interval not in KLINE_INTERVALS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported interval. Use one of: {sorted(KLINE_INTERVALS)}",
        )
    pair = _normalize_kline_symbol(symbol)
    max_limit = min(int(limit or KLINE_LIMIT_DEFAULT.get(interval, 200)), 1000)
    if max_limit < 10:
        max_limit = 10

    raw = await asyncio.to_thread(_fetch_klines_sync, pair, "spot", interval, max_limit)
    market = "spot"
    if not raw:
        raw = await asyncio.to_thread(
            _fetch_klines_sync, pair, "futures", interval, max_limit
        )
        market = "futures" if raw else "none"

    candles = _parse_binance_klines(raw or [])
    return {
        "symbol": pair.replace("USDT", ""),
        "pair": pair,
        "interval": interval,
        "market": market,
        "candles": candles,
        "count": len(candles),
        "ts": utc_now_iso(),
    }


@app.get("/")
async def index_page() -> FileResponse:
    """托管前端大屏页面。"""
    index = FRONTEND_DIR / "index.html"
    if not index.exists():
        raise HTTPException(status_code=404, detail=f"Frontend not found: {index}")
    return FileResponse(index)


if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await manager.connect(websocket)
    try:
        # 新客户端立即收到快照与系统状态
        await websocket.send_text(
            json.dumps(
                {
                    "type": "snapshot",
                    "prices": latest_prices,
                    "futures": futures_data,
                    "radar": {
                        "items": radar_state["items"],
                        "top3": radar_state.get("top3") or [],
                        "top3_fallback": bool(radar_state.get("top3_fallback")),
                        "btc_change_1h": radar_state["btc_change_1h"],
                        "source": radar_state["source"],
                        "simulated": radar_state["simulated"],
                        "updated_at": radar_state["updated_at"],
                    },
                    "market_safety": {
                        k: market_safety[k] for k in MARKET_SAFETY_KEYS
                    },
                    "multi_exchange": compute_aggregate(),
                    "exchange_connected": exchange_status["connected"],
                    "futures_status": futures_status,
                    "pump_bot": pump_bot.snapshot() if pump_bot is not None else None,
                    "alt_sim": alt_sim_bot.snapshot() if alt_sim_bot is not None else None,
                    "ts": utc_now_iso(),
                },
                ensure_ascii=False,
            )
        )
        await websocket.send_text(
            json.dumps(
                build_system_event(
                    "client_connected",
                    "前端已接入行情推送通道",
                ),
                ensure_ascii=False,
            )
        )

        while True:
            # 保持连接；前端可发送 ping / watch
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            mtype = msg.get("type")
            if mtype == "ping":
                await websocket.send_text(
                    json.dumps({"type": "pong", "ts": utc_now_iso()})
                )
            elif mtype == "watch":
                sym = str(msg.get("symbol") or "").upper().replace("USDT", "")
                if sym and sym not in ("BTC", "ETH"):
                    radar_state["watch_symbols"].add(sym)
                    ensure_price_slot(sym)
                    _get_radar_event().set()
                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": "system",
                                "event": "watch_ack",
                                "detail": f"已切换关注 {sym}，正在订阅实时流",
                                "symbol": sym,
                                "exchange_connected": exchange_status["connected"],
                                "ts": utc_now_iso(),
                            },
                            ensure_ascii=False,
                        )
                    )
    except WebSocketDisconnect:
        await manager.disconnect(websocket)
    except Exception as exc:
        logger.error("Frontend WS error: %s", exc)
        await manager.disconnect(websocket)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
