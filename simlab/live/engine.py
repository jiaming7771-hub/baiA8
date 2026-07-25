"""实盘引擎：前三强 → sizing → 限价入场 + 保护单。"""

from __future__ import annotations

import logging
from typing import Any

from simlab.live import config as C
from simlab.live import state as live_state
from simlab.live.exchange import (
    LiveSafetyError,
    cancel_open_orders,
    fetch_equity_usdt,
    place_limit_buy,
    place_stop_loss,
    place_take_profit,
    set_leverage_safe,
    to_swap_symbol,
)
from simlab.live.sizing import calc_position_size, trading_pool_budget
from simlab.live.signals import fetch_top3_signals
from simlab.live.sync import sync_fills_and_protect

logger = logging.getLogger("simlab.live.engine")


def _pool_used(state: dict[str, Any]) -> float:
    used = 0.0
    for p in (state.get("pending") or {}).values():
        used += float(p.get("notional") or 0)
    for p in (state.get("positions") or {}).values():
        used += float(p.get("notional") or 0)
    return used


async def run_live_cycle(exchange: Any, *, dry_run: bool) -> dict[str, Any]:
    """执行一轮实盘循环。dry_run=True 时绝不发真实订单。"""
    if live_state.kill_switch_active():
        logger.error("KILL_SWITCH 已激活 → 取消挂单并拒绝新开仓")
        try:
            await cancel_open_orders(exchange)
        except Exception:
            pass
        return {"ok": False, "reason": "kill_switch", "actions": []}

    if not dry_run:
        if not C.LIVE_TRADING:
            raise LiveSafetyError("拒绝真单：未设置 LIVE_TRADING=1")

    state = live_state.load_live_state()
    state["cycle"] = int(state.get("cycle") or 0) + 1

    equity = await fetch_equity_usdt(exchange)
    if equity <= 0:
        raise LiveSafetyError(f"权益无效: {equity}")

    actions: list[dict[str, Any]] = []
    actions.extend(
        await sync_fills_and_protect(exchange, state, dry_run=dry_run)
    )

    pool = trading_pool_budget(equity)
    used = _pool_used(state)
    remaining = max(0.0, pool - used)

    signals = fetch_top3_signals()
    top3 = signals.get("top3") or []
    top3_syms = {t["symbol"] for t in top3}

    actions: list[dict[str, Any]] = []

    # 掉出前三的挂单：取消
    for sym in list(state.get("pending") or {}):
        if sym not in top3_syms:
            info = state["pending"].pop(sym)
            ccxt_sym = info.get("ccxt_symbol")
            if ccxt_sym and not dry_run:
                try:
                    await cancel_open_orders(exchange, ccxt_sym)
                except Exception as exc:
                    logger.warning("cancel %s: %s", sym, exc)
            actions.append({"type": "cancel_pending", "symbol": sym, "reason": "left_top3"})
            live_state.append_order_log(
                {"event": "cancel_pending", "symbol": sym, "reason": "left_top3"}
            )

    open_n = len(state.get("positions") or {}) + len(state.get("pending") or {})

    for sig in top3:
        sym = sig["symbol"]
        if sym in (state.get("positions") or {}) or sym in (state.get("pending") or {}):
            continue
        if open_n >= C.MAX_OPEN_POSITIONS:
            actions.append({"type": "skip", "symbol": sym, "reason": "max_positions"})
            break

        entry = float(sig["entry"])
        stop = float(sig["stop_loss"])
        take = float(sig["take_profit"])
        size = calc_position_size(
            total_equity=equity,
            entry=entry,
            stop_loss=stop,
            pool_remaining=remaining,
        )
        if not size.ok:
            actions.append({"type": "skip", "symbol": sym, "reason": size.reason})
            continue

        try:
            ccxt_sym = to_swap_symbol(sym, exchange)
        except LiveSafetyError as exc:
            actions.append({"type": "skip", "symbol": sym, "reason": str(exc)})
            continue

        await set_leverage_safe(exchange, ccxt_sym, C.MAX_LEVERAGE)

        try:
            order = await place_limit_buy(
                exchange, ccxt_sym, size.qty, entry, dry_run=dry_run
            )
        except Exception as exc:
            logger.exception("place entry failed %s", sym)
            actions.append({"type": "error", "symbol": sym, "reason": str(exc)})
            continue

        # 保护单：若交易所支持附带 SL/TP 更好；这里分拆下达（入场未成交前部分交易所可能拒 reduceOnly）
        # 保守策略：先记录意图，下一轮持仓同步后再挂 SL/TP；dry-run 立即模拟挂出
        sl_order = tp_order = None
        if dry_run:
            sl_order = await place_stop_loss(
                exchange, ccxt_sym, size.qty, stop, dry_run=True
            )
            tp_order = await place_take_profit(
                exchange, ccxt_sym, size.qty, take, dry_run=True
            )

        state.setdefault("pending", {})[sym] = {
            "ccxt_symbol": ccxt_sym,
            "entry": entry,
            "stop_loss": stop,
            "take_profit": take,
            "qty": size.qty,
            "notional": size.notional,
            "risk_amount": size.risk_amount,
            "order_id": order.get("id"),
            "cycles_alive": 0,
            "created_at": live_state.utc_now(),
            "score": sig.get("total_score"),
            "hard_pass": sig.get("hard_pass"),
        }
        remaining = max(0.0, remaining - size.notional)
        open_n += 1
        actions.append(
            {
                "type": "entry_limit",
                "symbol": sym,
                "qty": size.qty,
                "entry": entry,
                "stop_loss": stop,
                "take_profit": take,
                "risk_amount": size.risk_amount,
                "notional": size.notional,
                "dry_run": dry_run,
                "order_id": order.get("id"),
            }
        )
        live_state.append_order_log(
            {
                "event": "entry_limit",
                "symbol": sym,
                "order": order,
                "sl": sl_order,
                "tp": tp_order,
                "dry_run": dry_run,
                "risk_amount": size.risk_amount,
                "equity": equity,
            }
        )
        logger.info(
            "%s ENTRY %s qty=%.6f entry=%.6f sl=%.6f tp=%.6f risk=%.4fU notional=%.2fU",
            "[DRY]" if dry_run else "[LIVE]",
            sym,
            size.qty,
            entry,
            stop,
            take,
            size.risk_amount,
            size.notional,
        )

    # TTL：挂单老化取消
    for sym, pend in list((state.get("pending") or {}).items()):
        pend["cycles_alive"] = int(pend.get("cycles_alive") or 0) + 1
        if pend["cycles_alive"] >= C.PENDING_TTL_CYCLES:
            state["pending"].pop(sym, None)
            if not dry_run and pend.get("ccxt_symbol"):
                try:
                    await cancel_open_orders(exchange, pend["ccxt_symbol"])
                except Exception:
                    pass
            actions.append({"type": "cancel_pending", "symbol": sym, "reason": "ttl"})

    state["last_equity"] = equity
    state["last_pool"] = pool
    state["last_top3"] = [t.get("symbol") for t in top3]
    live_state.save_live_state(state)

    return {
        "ok": True,
        "cycle": state["cycle"],
        "equity": equity,
        "pool": pool,
        "pool_remaining": remaining,
        "risk_percent": C.RISK_PERCENT,
        "pool_fraction": C.POOL_FRACTION,
        "sandbox": C.SANDBOX,
        "dry_run": dry_run,
        "top3": [t.get("symbol") for t in top3],
        "actions": actions,
        "signals": signals,
    }
