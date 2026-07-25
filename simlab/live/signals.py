"""从雷达/评分引擎获取前三强交易信号。"""

from __future__ import annotations

import logging
from typing import Any

from simlab.levels import calculate_advanced_trading_levels
from simlab.live import config as C
from simlab.market.klines import load_mtf_frames
from simlab.scoring.ranker import rank_ambush_rotation
from simlab.screener import screen_top10

logger = logging.getLogger("simlab.live.signals")


def fetch_top3_signals() -> dict[str, Any]:
    """产出仅用于实盘的前三强信号（默认要求 hard_pass）。"""
    screen = screen_top10()
    items = screen.get("items") or []
    prepared: list[dict[str, Any]] = []
    for c in items:
        frames = load_mtf_frames(c["pair"], 120)
        levels = calculate_advanced_trading_levels(
            frames["df_4h"], frames["df_1h"], frames["df_15m"]
        )
        if not levels:
            continue
        vol = (
            frames["df_15m"]["volume"]
            if "volume" in frames["df_15m"].columns
            else None
        )
        prepared.append(
            {
                **c,
                "vs_btc": c.get("vs_btc_24h") or c.get("vs_btc_1h") or 0,
                "levels": levels,
                "volume_15m": vol,
            }
        )

    ranked = rank_ambush_rotation(prepared) if prepared else {
        "top10": [],
        "top3": [],
        "top3_fallback": False,
        "passed_count": 0,
    }

    top3 = list(ranked.get("top3") or [])
    if not C.ALLOW_FALLBACK:
        top3 = [t for t in top3 if t.get("hard_pass") and not t.get("is_fallback")]

    # 结构校验
    clean: list[dict[str, Any]] = []
    for t in top3:
        entry = float(t.get("entry") or 0)
        stop = float(t.get("stop_loss") or 0)
        take = float(t.get("take_profit") or 0)
        price = float(t.get("price") or 0)
        if not (stop < entry < take) or entry <= 0 or price <= 0:
            logger.warning("skip %s invalid levels", t.get("symbol"))
            continue
        if C.REQUIRE_PRICE_ABOVE_ENTRY and price <= entry:
            logger.info("skip %s price<=entry（不追高）", t.get("symbol"))
            continue
        clean.append(t)

    return {
        "top3": clean[:3],
        "top10": ranked.get("top10") or [],
        "top3_fallback": ranked.get("top3_fallback"),
        "screen": screen,
        "passed_count": ranked.get("passed_count"),
    }
