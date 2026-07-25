"""点位可操作性评估：距离 / 风险距离 / 盈亏比 → 可操作得分。"""

from __future__ import annotations

from typing import Any

from simlab.scoring import weights as W


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def distance_pct(price: float, entry: float) -> float | None:
    """现价到入场价的下方距离百分比（做多埋伏：price > entry）。

    返回 (price - entry) / price * 100；若现价不高于入场则返回负值/异常由调用方处理。
    """
    if price is None or entry is None or price <= 0:
        return None
    return (float(price) - float(entry)) / float(price) * 100.0


def risk_distance(entry: float, stop_loss: float) -> float | None:
    """风险距离：(entry - stop_loss) / entry。"""
    if entry is None or stop_loss is None or entry <= 0:
        return None
    return (float(entry) - float(stop_loss)) / float(entry)


def risk_reward_ratio(entry: float, stop_loss: float, take_profit: float) -> float | None:
    """盈亏比：(take_profit - entry) / (entry - stop_loss)。"""
    if entry is None or stop_loss is None or take_profit is None:
        return None
    risk = float(entry) - float(stop_loss)
    if risk <= 0:
        return None
    return (float(take_profit) - float(entry)) / risk


def score_distance(dist_pct: float) -> float:
    """距离得分：理想区间满分，过近/过远线性扣分。"""
    if dist_pct is None:
        return 0.0
    d = float(dist_pct)
    if W.DIST_IDEAL_LO <= d <= W.DIST_IDEAL_HI:
        return 100.0
    if d < W.DIST_IDEAL_LO:
        # hard_lo → 0，ideal_lo → 100
        if d <= W.DIST_HARD_LO:
            # 硬门槛以下继续衰减
            return _clamp(40.0 * max(d, 0.0) / W.DIST_HARD_LO)
        span = W.DIST_IDEAL_LO - W.DIST_HARD_LO
        return _clamp(40.0 + 60.0 * (d - W.DIST_HARD_LO) / span) if span > 0 else 0.0
    # d > ideal_hi
    if d >= W.DIST_HARD_HI:
        return 0.0
    span = W.DIST_HARD_HI - W.DIST_IDEAL_HI
    return _clamp(100.0 * (W.DIST_HARD_HI - d) / span) if span > 0 else 0.0


def score_risk_reward(rr: float | None) -> float:
    """盈亏比附加分：RR 越高越好，约在 2.0 附近封顶。"""
    if rr is None or rr <= 0:
        return 0.0
    if rr >= W.RR_SCORE_IDEAL:
        return 100.0
    if rr <= W.RR_SCORE_FLOOR:
        return _clamp(40.0 * rr / W.RR_SCORE_FLOOR)
    span = W.RR_SCORE_IDEAL - W.RR_SCORE_FLOOR
    return _clamp(40.0 + 60.0 * (rr - W.RR_SCORE_FLOOR) / span)


def evaluate_operability(
    price: float,
    entry: float,
    stop_loss: float,
    take_profit: float,
) -> dict[str, Any]:
    """评估点位可操作性，返回指标与融合得分（0~100）。"""
    dist = distance_pct(price, entry)
    risk = risk_distance(entry, stop_loss)
    rr = risk_reward_ratio(entry, stop_loss, take_profit)

    dist_score = score_distance(dist) if dist is not None else 0.0
    rr_score = score_risk_reward(rr)

    # 距离与盈亏比加权融合；风险过大时额外扣分
    fused = W.OP_W_DISTANCE * dist_score + W.OP_W_RR * rr_score
    if risk is not None and risk > W.HARD_RISK_MAX:
        # 超过硬门槛后按超额比例扣分（最多扣 30 分）
        over = (risk - W.HARD_RISK_MAX) / max(W.HARD_RISK_MAX, 1e-9)
        fused -= min(30.0, over * 30.0)

    operability = round(_clamp(fused), 2)
    return {
        "distance_pct": None if dist is None else round(dist, 4),
        "risk_distance": None if risk is None else round(risk, 6),
        "risk_reward_ratio": None if rr is None else round(rr, 4),
        "distance_score": round(dist_score, 2),
        "rr_score": round(rr_score, 2),
        "operability_score": operability,
    }
