"""币安行情适配。"""

from __future__ import annotations

from typing import Any

from simlab import config
from simlab.market.http import fetch_json


def fetch_perp_bases() -> tuple[set[str], str]:
    """USDT 永续 bases；合约不可达时退化到现货交易对。"""
    info = fetch_json("/fapi/v1/exchangeInfo", config.BINANCE_FUTURES_HOSTS)
    bases: set[str] = set()
    if info and info.get("symbols"):
        for s in info["symbols"]:
            if s.get("contractType") not in (None, "PERPETUAL"):
                continue
            if s.get("quoteAsset") != "USDT":
                continue
            if s.get("status") not in (None, "TRADING"):
                continue
            sym = s.get("symbol") or ""
            if sym.endswith("USDT"):
                base = sym[:-4]
                if base and base not in config.EXCLUDE_BASES:
                    bases.add(base)
        if bases:
            return bases, "binance_perp"

    info = fetch_json("/api/v3/exchangeInfo", config.BINANCE_SPOT_HOSTS)
    for s in (info or {}).get("symbols") or []:
        if s.get("status") != "TRADING" or s.get("quoteAsset") != "USDT":
            continue
        sym = s.get("symbol") or ""
        if sym.endswith("USDT"):
            base = sym[:-4]
            if base and base not in config.EXCLUDE_BASES:
                bases.add(base)
    return bases, "binance_spot_proxy"


def fetch_futures_24hr() -> list[dict[str, Any]]:
    data = fetch_json("/fapi/v1/ticker/24hr", config.BINANCE_FUTURES_HOSTS)
    return data if isinstance(data, list) else []


def fetch_premium_index() -> dict[str, float]:
    data = fetch_json("/fapi/v1/premiumIndex", config.BINANCE_FUTURES_HOSTS)
    out: dict[str, float] = {}
    for row in data or []:
        sym = row.get("symbol")
        if not sym:
            continue
        try:
            out[sym] = float(row.get("lastFundingRate") or 0)
        except (TypeError, ValueError):
            continue
    return out


def fetch_spot_24hr() -> dict[str, dict[str, Any]]:
    data = fetch_json("/api/v3/ticker/24hr", config.BINANCE_SPOT_HOSTS)
    out: dict[str, dict[str, Any]] = {}
    for row in data or []:
        sym = row.get("symbol") or ""
        if sym.endswith("USDT"):
            out[sym[:-4]] = row
    return out


def fetch_klines(pair: str, interval: str, limit: int = 120) -> list | None:
    path = f"/api/v3/klines?symbol={pair}&interval={interval}&limit={limit}"
    data = fetch_json(path, config.BINANCE_SPOT_HOSTS)
    if isinstance(data, list) and data:
        return data
    path = f"/fapi/v1/klines?symbol={pair}&interval={interval}&limit={limit}"
    data = fetch_json(path, config.BINANCE_FUTURES_HOSTS)
    return data if isinstance(data, list) else None


def fetch_ticker_price(pair: str) -> float | None:
    data = fetch_json(f"/api/v3/ticker/price?symbol={pair}", config.BINANCE_SPOT_HOSTS)
    if data and "price" in data:
        try:
            return float(data["price"])
        except (TypeError, ValueError):
            return None
    data = fetch_json(f"/fapi/v1/ticker/price?symbol={pair}", config.BINANCE_FUTURES_HOSTS)
    if data and "price" in data:
        try:
            return float(data["price"])
        except (TypeError, ValueError):
            return None
    return None
