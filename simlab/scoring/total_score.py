"""各维度得分 + 综合总分。"""

from __future__ import annotations

import math
from typing import Any

from simlab.scoring import weights as W
from simlab.scoring.operability import evaluate_operability


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def score_volume_log(quote_volume: float) -> float:
    """成交额对数归一化 → 0~100（基准 5000万 ~ 10亿）。"""
    v = max(float(quote_volume or 0), 1.0)
    lo = math.log10(W.VOL_LOG_FLOOR)
    hi = math.log10(W.VOL_LOG_CEIL)
    return round(_clamp01((math.log10(v) - lo) / (hi - lo)) * 100.0, 2)


def score_rel_strength(vs_btc_pct: float) -> float:
    """相对强度得分：vs BTC 落在 -5%~+15% 区间线性映射。"""
    x = float(vs_btc_pct or 0.0)
    return round(_clamp01((x - W.RS_FLOOR) / (W.RS_CEIL - W.RS_FLOOR)) * 100.0, 2)


def score_funding_health(funding_rate: float | None) -> float:
    """资金费率健康度：越接近 0 越高；缺失给中性分 50。"""
    if funding_rate is None:
        return 50.0
    abs_fr = abs(float(funding_rate))
    return round(_clamp01(1.0 - abs_fr / W.MAX_ABS_FUNDING) * 100.0, 2)


def score_atr_quality(atr: float | None, price: float) -> float:
    """波动率质量：ATR% 落在 3%~8% 理想，过低过高扣分。"""
    if atr is None or price is None or price <= 0:
        return 0.0
    atr_pct = float(atr) / float(price) * 100.0
    if W.ATR_PCT_IDEAL_LO <= atr_pct <= W.ATR_PCT_IDEAL_HI:
        return 100.0
    if atr_pct < W.ATR_PCT_IDEAL_LO:
        if atr_pct <= W.ATR_PCT_HARD_LO:
            return round(30.0 * max(atr_pct, 0.0) / W.ATR_PCT_HARD_LO, 2)
        span = W.ATR_PCT_IDEAL_LO - W.ATR_PCT_HARD_LO
        return round(30.0 + 70.0 * (atr_pct - W.ATR_PCT_HARD_LO) / span, 2)
    if atr_pct >= W.ATR_PCT_HARD_HI:
        return 0.0
    span = W.ATR_PCT_HARD_HI - W.ATR_PCT_IDEAL_HI
    return round(100.0 * (W.ATR_PCT_HARD_HI - atr_pct) / span, 2)


def compute_total_score(
    *,
    quote_volume: float,
    vs_btc_pct: float,
    funding_rate: float | None,
    price: float,
    entry: float,
    stop_loss: float,
    take_profit: float,
    atr: float | None,
) -> dict[str, Any]:
    """综合评分模型：五维加权 → total_score（0~100）。"""
    s_vol = score_volume_log(quote_volume)
    s_rs = score_rel_strength(vs_btc_pct)
    s_fund = score_funding_health(funding_rate)
    s_atr = score_atr_quality(atr, price)
    op = evaluate_operability(price, entry, stop_loss, take_profit)
    s_op = float(op["operability_score"])

    total = (
        W.W_VOLUME * s_vol
        + W.W_REL_STRENGTH * s_rs
        + W.W_FUNDING * s_fund
        + W.W_VOLATILITY * s_atr
        + W.W_OPERABILITY * s_op
    )
    atr_pct = None
    if atr is not None and price > 0:
        atr_pct = round(float(atr) / float(price) * 100.0, 4)

    return {
        "total_score": round(total, 2),
        "score_volume": s_vol,
        "score_rel_strength": s_rs,
        "score_funding": s_fund,
        "score_volatility": s_atr,
        "score_operability": s_op,
        "atr_pct": atr_pct,
        "operability": op,
        "weights": {
            "volume": W.W_VOLUME,
            "rel_strength": W.W_REL_STRENGTH,
            "funding": W.W_FUNDING,
            "volatility": W.W_VOLATILITY,
            "operability": W.W_OPERABILITY,
        },
    }
