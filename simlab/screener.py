"""双子星 TOP10 选币雷达。"""

from __future__ import annotations

import logging
from typing import Any

from simlab import config
from simlab.market import binance as bn
from simlab.market import okx as ox

logger = logging.getLogger("simlab.screener")


def _norm_minmax(values: list[float]) -> list[float]:
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi - lo < 1e-12:
        return [0.5 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]


def _funding_health(fr: float | None) -> float:
    if fr is None:
        return 0.5
    return max(0.0, 1.0 - abs(float(fr)) / config.MAX_ABS_FUNDING)


def twin_star_whitelist() -> tuple[set[str], dict[str, Any]]:
    okx_bases = ox.fetch_swap_bases()
    bin_bases, bin_src = bn.fetch_perp_bases()
    twin = okx_bases & bin_bases
    meta = {
        "okx_count": len(okx_bases),
        "binance_count": len(bin_bases),
        "twin_count": len(twin),
        "binance_source": bin_src,
    }
    logger.info(
        "whitelist OKX=%s Binance(%s)=%s ∩=%s",
        meta["okx_count"],
        bin_src,
        meta["binance_count"],
        meta["twin_count"],
    )
    return twin, meta


def screen_top10() -> dict[str, Any]:
    """执行一次双子星筛选，返回 TOP10 与元数据。"""
    twin, meta = twin_star_whitelist()
    candidates: list[dict[str, Any]] = []

    futures = bn.fetch_futures_24hr()
    funding_map = bn.fetch_premium_index()
    spot_map = bn.fetch_spot_24hr()
    source = "binance_futures"

    btc_chg_24h = 0.0
    if "BTC" in spot_map:
        try:
            btc_chg_24h = float(spot_map["BTC"].get("priceChangePercent") or 0)
        except (TypeError, ValueError):
            btc_chg_24h = 0.0

    if futures and funding_map:
        for t in futures:
            pair = t.get("symbol") or ""
            if not pair.endswith("USDT"):
                continue
            base = pair[:-4]
            if base in config.EXCLUDE_BASES or (twin and base not in twin):
                continue
            try:
                vol = float(t.get("quoteVolume") or 0)
                price = float(t.get("lastPrice") or 0)
                chg = float(t.get("priceChangePercent") or 0)
            except (TypeError, ValueError):
                continue
            if vol < config.MIN_QUOTE_VOLUME or price <= 0:
                continue
            fr = funding_map.get(pair)
            if fr is not None and abs(fr) >= config.MAX_ABS_FUNDING:
                continue
            if chg <= btc_chg_24h:
                continue
            candidates.append(
                {
                    "symbol": base,
                    "pair": pair,
                    "price": price,
                    "quote_volume": vol,
                    "change_pct_24h": chg,
                    "vs_btc_24h": chg - btc_chg_24h,
                    "funding_rate": fr,
                    "okx_inst": f"{base}-USDT-SWAP",
                }
            )
    else:
        source = "okx_swap+binance_spot"
        if "BTC" in spot_map:
            try:
                btc_chg_24h = float(spot_map["BTC"].get("priceChangePercent") or 0)
            except (TypeError, ValueError):
                pass
        for t in ox.fetch_swap_tickers():
            inst = t.get("instId") or ""
            base = ox.parse_usdt_swap_base(inst)
            if not base or (twin and base not in twin):
                continue
            if twin and meta.get("binance_source") == "binance_spot_proxy":
                if base not in spot_map:
                    continue
            try:
                vol = float(t.get("volCcy24h") or 0)
                price = float(t.get("last") or 0)
            except (TypeError, ValueError):
                continue
            spot = spot_map.get(base)
            chg = 0.0
            if spot:
                try:
                    price = float(spot.get("lastPrice") or price)
                    vol = max(vol, float(spot.get("quoteVolume") or 0))
                    chg = float(spot.get("priceChangePercent") or 0)
                except (TypeError, ValueError):
                    pass
            if vol < config.MIN_QUOTE_VOLUME or price <= 0:
                continue
            if chg <= btc_chg_24h:
                continue
            fr = ox.fetch_funding(inst)
            if fr is not None and abs(fr) >= config.MAX_ABS_FUNDING:
                continue
            candidates.append(
                {
                    "symbol": base,
                    "pair": f"{base}USDT",
                    "price": price,
                    "quote_volume": vol,
                    "change_pct_24h": chg,
                    "vs_btc_24h": chg - btc_chg_24h,
                    "funding_rate": fr,
                    "okx_inst": inst,
                }
            )

    if not candidates:
        return {
            "items": [],
            "btc_change_24h": btc_chg_24h,
            "source": source,
            "twin_meta": meta,
            "candidate_count": 0,
        }

    vols = [c["quote_volume"] for c in candidates]
    rss = [c["vs_btc_24h"] for c in candidates]
    vol_n = _norm_minmax(vols)
    rs_n = _norm_minmax(rss)

    scored: list[dict[str, Any]] = []
    for i, c in enumerate(candidates):
        fh = _funding_health(c.get("funding_rate"))
        score = (
            vol_n[i] * config.SCORE_W_VOLUME
            + rs_n[i] * config.SCORE_W_RS
            + fh * config.SCORE_W_FUNDING
        )
        c["score"] = round(score, 6)
        c["funding_health"] = round(fh, 4)
        scored.append(c)

    scored.sort(key=lambda x: x["score"], reverse=True)
    top = scored[: config.TOP_N]
    for i, c in enumerate(top, 1):
        c["rank"] = i

    logger.info(
        "screen source=%s candidates=%s top=%s btc24h=%.3f%%",
        source,
        len(candidates),
        [f"{x['symbol']}:{x['score']:.3f}" for x in top],
        btc_chg_24h,
    )
    return {
        "items": top,
        "btc_change_24h": btc_chg_24h,
        "source": source,
        "twin_meta": meta,
        "candidate_count": len(candidates),
    }
