"""组合估值与仓位 sizing。"""

from __future__ import annotations

from typing import Any

from simlab import config


def mark_equity(state: dict[str, Any], marks: dict[str, float]) -> dict[str, float]:
    """用最新价估值持仓，返回权益快照字段。"""
    cash = float(state.get("cash") or 0)
    unreal = 0.0
    for sym, pos in (state.get("positions") or {}).items():
        px = marks.get(sym)
        if px is None:
            px = float(pos.get("mark") or pos.get("entry_price") or 0)
        qty = float(pos.get("qty") or 0)
        entry = float(pos.get("entry_price") or 0)
        unreal += (px - entry) * qty
        pos["mark"] = px
        pos["unrealized_pnl"] = round((px - entry) * qty, 6)
    equity = cash + sum(
        float(p.get("mark") or p.get("entry_price") or 0) * float(p.get("qty") or 0)
        for p in (state.get("positions") or {}).values()
    )
    return {
        "cash": round(cash, 6),
        "equity": round(equity, 6),
        "equity_start": float(state.get("equity_start") or config.INITIAL_EQUITY),
        "realized_pnl": round(float(state.get("realized_pnl") or 0), 6),
        "unrealized_pnl": round(unreal, 6),
        "fees_paid": round(float(state.get("fees_paid") or 0), 6),
        "open_positions": len(state.get("positions") or {}),
        "pending_orders": len(state.get("pending") or {}),
        "wins": int(state.get("wins") or 0),
        "losses": int(state.get("losses") or 0),
    }


def size_long(
    equity: float,
    entry: float,
    stop: float,
    cash: float,
) -> float:
    """按风险预算计算多头数量；受单仓上限与可用现金约束。"""
    if entry <= 0 or stop <= 0 or entry <= stop or equity <= 0:
        return 0.0
    risk_per_unit = entry - stop
    risk_budget = equity * config.RISK_PER_TRADE
    qty_by_risk = risk_budget / risk_per_unit
    max_notional = equity * config.MAX_POSITION_PCT
    qty_by_cap = max_notional / entry
    qty_by_cash = (cash * 0.98) / entry  # 预留手续费
    qty = min(qty_by_risk, qty_by_cap, qty_by_cash)
    return max(0.0, qty)
