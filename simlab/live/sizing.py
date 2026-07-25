"""仓位 sizing：严格按风险金额 / (entry-stop)。"""

from __future__ import annotations

from dataclasses import dataclass

from simlab.live import config as C


@dataclass
class SizeResult:
    ok: bool
    qty: float
    notional: float
    risk_amount: float
    reason: str = ""


def calc_position_size(
    *,
    total_equity: float,
    entry: float,
    stop_loss: float,
    pool_remaining: float,
) -> SizeResult:
    """极度保守仓位计算。

    risk_amount = total_equity * risk_percent
    position_size = risk_amount / (entry - stop_loss)
    再受资金池剩余、单仓名义上限约束。
    """
    if total_equity <= 0 or entry <= 0 or stop_loss <= 0:
        return SizeResult(False, 0.0, 0.0, 0.0, "无效权益或点位")
    if entry <= stop_loss:
        return SizeResult(False, 0.0, 0.0, 0.0, "entry 必须高于 stop_loss")

    risk_pct = C.RISK_PERCENT
    if risk_pct > 0.01:
        return SizeResult(False, 0.0, 0.0, 0.0, "风险比例超过硬顶 1%")

    risk_amount = float(total_equity) * float(risk_pct)
    stop_dist = float(entry) - float(stop_loss)
    qty = risk_amount / stop_dist
    notional = qty * float(entry)

    # 资金池剩余
    if pool_remaining <= 0:
        return SizeResult(False, 0.0, 0.0, risk_amount, "可交易资金池已用尽")
    if notional > pool_remaining:
        qty = pool_remaining / float(entry)
        notional = qty * float(entry)
        # 缩仓后重新校验风险不超过设定（允许变小）
        risk_amount = qty * stop_dist

    # 单仓名义上限
    max_notional = float(total_equity) * float(C.MAX_NOTIONAL_FRACTION)
    if notional > max_notional:
        qty = max_notional / float(entry)
        notional = qty * float(entry)
        risk_amount = qty * stop_dist

    if notional < C.MIN_NOTIONAL_USDT:
        return SizeResult(False, 0.0, 0.0, risk_amount, f"名义过小 <{C.MIN_NOTIONAL_USDT}U")

    # 最终风险再校验：不得超过权益 1%
    if risk_amount > total_equity * 0.01 + 1e-9:
        return SizeResult(False, 0.0, 0.0, risk_amount, "最终风险超过权益 1%")

    return SizeResult(True, qty, notional, risk_amount, "ok")


def trading_pool_budget(total_equity: float) -> float:
    frac = min(float(C.POOL_FRACTION), 0.20)
    return float(total_equity) * frac
