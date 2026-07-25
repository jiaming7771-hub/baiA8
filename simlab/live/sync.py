"""持仓/挂单与交易所同步（保护单补挂）。"""

from __future__ import annotations

import logging
from typing import Any

from simlab.live import config as C
from simlab.live import state as live_state
from simlab.live.exchange import place_stop_loss, place_take_profit

logger = logging.getLogger("simlab.live.sync")


async def sync_fills_and_protect(
    exchange: Any,
    state: dict[str, Any],
    *,
    dry_run: bool,
) -> list[dict[str, Any]]:
    """若限价已成交，将 pending 转入 positions，并补挂 SL/TP。"""
    actions: list[dict[str, Any]] = []
    try:
        positions = await exchange.fetch_positions()
    except Exception as exc:
        logger.warning("fetch_positions failed: %s", exc)
        return actions

    pos_by_sym: dict[str, dict[str, Any]] = {}
    for p in positions or []:
        try:
            contracts = float(p.get("contracts") or p.get("contractSize") or 0)
            # ccxt 统一：contracts 为持仓张数/币数
            qty = float(p.get("contracts") or 0)
            if abs(qty) < 1e-12:
                continue
            symbol = p.get("symbol") or ""
            pos_by_sym[symbol] = p
        except (TypeError, ValueError):
            continue

    for base, pend in list((state.get("pending") or {}).items()):
        ccxt_sym = pend.get("ccxt_symbol")
        if not ccxt_sym or ccxt_sym not in pos_by_sym:
            continue
        p = pos_by_sym[ccxt_sym]
        qty = abs(float(p.get("contracts") or pend.get("qty") or 0))
        entry = float(p.get("entryPrice") or pend.get("entry") or 0)
        stop = float(pend["stop_loss"])
        take = float(pend["take_profit"])

        # 补挂保护单（仅一次）
        if not pend.get("protect_placed"):
            try:
                await place_stop_loss(exchange, ccxt_sym, qty, stop, dry_run=dry_run)
                await place_take_profit(exchange, ccxt_sym, qty, take, dry_run=dry_run)
                pend["protect_placed"] = True
            except Exception as exc:
                logger.error("保护单失败 %s: %s — 请立即手工挂止损", base, exc)
                actions.append({"type": "protect_error", "symbol": base, "reason": str(exc)})

        state.setdefault("positions", {})[base] = {
            "ccxt_symbol": ccxt_sym,
            "qty": qty,
            "entry_price": entry,
            "stop_loss": stop,
            "take_profit": take,
            "notional": qty * entry,
            "opened_at": live_state.utc_now(),
        }
        state["pending"].pop(base, None)
        actions.append({"type": "filled", "symbol": base, "qty": qty, "entry": entry})
        live_state.append_order_log(
            {"event": "filled", "symbol": base, "qty": qty, "entry": entry}
        )
        logger.info("FILLED %s qty=%s @%s — 已请求 SL/TP", base, qty, entry)

    return actions
