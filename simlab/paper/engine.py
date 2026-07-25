"""15 分钟模拟盘撮合引擎：挂单 / 成交 / 止损 / 止盈。"""

from __future__ import annotations

import logging
from typing import Any

from simlab import config
from simlab.levels import calculate_advanced_trading_levels
from simlab.market import binance as bn
from simlab.market.klines import latest_ohlc_15m, load_mtf_frames
from simlab.paper.portfolio import mark_equity, size_long
from simlab.scoring.ranker import rank_ambush_rotation
from simlab.screener import screen_top10
from simlab.storage import state as store

logger = logging.getLogger("simlab.engine")


def _apply_slip(price: float, side: str) -> float:
    bps = config.SLIPPAGE_BPS / 10_000.0
    if side == "buy":
        return price * (1.0 + bps)
    return price * (1.0 - bps)


def _fee(notional: float) -> float:
    return abs(notional) * config.FEE_RATE


def _close_position(
    state: dict[str, Any],
    symbol: str,
    exit_price: float,
    reason: str,
) -> None:
    pos = (state.get("positions") or {}).pop(symbol, None)
    if not pos:
        return
    qty = float(pos["qty"])
    entry = float(pos["entry_price"])
    px = _apply_slip(exit_price, "sell")
    notional = qty * px
    fee = _fee(notional)
    pnl = (px - entry) * qty - fee - float(pos.get("entry_fee") or 0)
    state["cash"] = float(state["cash"]) + notional - fee
    state["realized_pnl"] = float(state.get("realized_pnl") or 0) + pnl
    state["fees_paid"] = float(state.get("fees_paid") or 0) + fee
    if pnl >= 0:
        state["wins"] = int(state.get("wins") or 0) + 1
    else:
        state["losses"] = int(state.get("losses") or 0) + 1
    store.append_trade(
        {
            "event": "close",
            "symbol": symbol,
            "reason": reason,
            "qty": qty,
            "entry_price": entry,
            "exit_price": px,
            "pnl": round(pnl, 6),
            "fee": round(fee, 6),
        }
    )
    store.append_event(
        {"type": "close", "symbol": symbol, "reason": reason, "pnl": round(pnl, 6)}
    )
    logger.info("CLOSE %s reason=%s pnl=%.4f @%.6f", symbol, reason, pnl, px)


def _fill_pending(
    state: dict[str, Any],
    symbol: str,
    bar: dict[str, float],
) -> None:
    pending = (state.get("pending") or {}).get(symbol)
    if not pending:
        return
    entry = float(pending["entry"])
    # 限价多：当根 15m 最低价触及入场
    if bar["low"] > entry:
        pending["cycles_alive"] = int(pending.get("cycles_alive") or 0) + 1
        if pending["cycles_alive"] >= config.PENDING_TTL_CYCLES:
            (state["pending"] or {}).pop(symbol, None)
            store.append_event({"type": "cancel", "symbol": symbol, "reason": "ttl"})
            logger.info("CANCEL %s ttl", symbol)
        return

    if symbol in (state.get("positions") or {}):
        (state["pending"] or {}).pop(symbol, None)
        return

    qty = float(pending["qty"])
    px = _apply_slip(entry, "buy")
    notional = qty * px
    fee = _fee(notional)
    if notional + fee > float(state["cash"]):
        (state["pending"] or {}).pop(symbol, None)
        store.append_event({"type": "cancel", "symbol": symbol, "reason": "insufficient_cash"})
        return

    state["cash"] = float(state["cash"]) - notional - fee
    state.setdefault("positions", {})[symbol] = {
        "qty": qty,
        "entry_price": px,
        "stop_loss": float(pending["stop_loss"]),
        "take_profit": float(pending["take_profit"]),
        "defense": float(pending.get("defense") or 0),
        "entry_fee": fee,
        "opened_at": store.utc_now(),
        "score": pending.get("score"),
    }
    (state["pending"] or {}).pop(symbol, None)
    state["fees_paid"] = float(state.get("fees_paid") or 0) + fee
    store.append_trade(
        {
            "event": "open",
            "symbol": symbol,
            "qty": qty,
            "entry_price": px,
            "stop_loss": pending["stop_loss"],
            "take_profit": pending["take_profit"],
            "fee": round(fee, 6),
        }
    )
    store.append_event({"type": "open", "symbol": symbol, "price": px, "qty": qty})
    logger.info("OPEN %s qty=%.6f @%.6f", symbol, qty, px)


def _manage_open(state: dict[str, Any], symbol: str, bar: dict[str, float]) -> None:
    pos = (state.get("positions") or {}).get(symbol)
    if not pos:
        return
    stop = float(pos["stop_loss"])
    take = float(pos["take_profit"])
    # 同根 K 线先触及止损优先（保守）
    if bar["low"] <= stop:
        _close_position(state, symbol, stop, "stop_loss")
        return
    if bar["high"] >= take:
        _close_position(state, symbol, take, "take_profit")


def _place_or_refresh_pending(
    state: dict[str, Any],
    coin: dict[str, Any],
    levels: dict[str, Any],
    equity: float,
) -> None:
    symbol = coin["symbol"]
    if symbol in (state.get("positions") or {}):
        return
    open_n = len(state.get("positions") or {})
    pending_n = len(state.get("pending") or {})
    # 已有挂单：刷新点位，保留 qty
    if symbol in (state.get("pending") or {}):
        p = state["pending"][symbol]
        p.update(
            {
                "entry": levels["entry"],
                "stop_loss": levels["stop_loss"],
                "take_profit": levels["take_profit"],
                "defense": levels["defense"],
                "lower_band": levels["lower_band"],
                "atr": levels["atr"],
                "score": coin.get("score"),
                "updated_at": store.utc_now(),
            }
        )
        return

    if open_n + pending_n >= config.MAX_OPEN_POSITIONS:
        return

    price = float(coin.get("price") or 0)
    entry = float(levels["entry"])
    stop = float(levels["stop_loss"])
    if config.REQUIRE_PRICE_ABOVE_ENTRY and price <= entry:
        return

    qty = size_long(equity, entry, stop, float(state["cash"]))
    if qty * entry < 10:  # 名义过小忽略
        return

    state.setdefault("pending", {})[symbol] = {
        "entry": entry,
        "stop_loss": stop,
        "take_profit": float(levels["take_profit"]),
        "defense": float(levels["defense"]),
        "lower_band": float(levels["lower_band"]),
        "atr": float(levels["atr"]),
        "qty": qty,
        "score": coin.get("score"),
        "cycles_alive": 0,
        "created_at": store.utc_now(),
    }
    store.append_event(
        {
            "type": "pending",
            "symbol": symbol,
            "entry": entry,
            "stop_loss": stop,
            "take_profit": levels["take_profit"],
            "qty": qty,
        }
    )
    logger.info(
        "PENDING %s entry=%.6f sl=%.6f tp=%.6f qty=%.6f",
        symbol,
        entry,
        stop,
        levels["take_profit"],
        qty,
    )


def run_cycle(once_marks: bool = True) -> dict[str, Any]:
    """执行一轮 15m 模拟盘循环。"""
    state = store.load_state()
    state["cycle"] = int(state.get("cycle") or 0) + 1

    screen = screen_top10()
    top = screen.get("items") or []
    top_syms = {c["symbol"] for c in top}

    # 1) 先对已有持仓/挂单用最新 15m bar 撮合
    watch = set(state.get("positions") or {}) | set(state.get("pending") or {})
    marks: dict[str, float] = {}
    for sym in list(watch):
        pair = f"{sym}USDT"
        bar = latest_ohlc_15m(pair)
        if not bar:
            px = bn.fetch_ticker_price(pair)
            if px:
                marks[sym] = px
            continue
        marks[sym] = bar["close"]
        _manage_open(state, sym, bar)
        _fill_pending(state, sym, bar)

    # 2) TOP10 算点位并挂单；掉出榜的挂单取消
    for sym in list(state.get("pending") or {}):
        if sym not in top_syms:
            state["pending"].pop(sym, None)
            store.append_event({"type": "cancel", "symbol": sym, "reason": "left_top10"})

    snap = mark_equity(state, marks)
    equity = snap["equity"]

    def _reserved_notional() -> float:
        reserved = 0.0
        for p in (state.get("pending") or {}).values():
            reserved += float(p.get("qty") or 0) * float(p.get("entry") or 0)
        for p in (state.get("positions") or {}).values():
            reserved += float(p.get("qty") or 0) * float(
                p.get("mark") or p.get("entry_price") or 0
            )
        return reserved

    levels_map: dict[str, Any] = {}
    prepared: list[dict[str, Any]] = []
    for coin in top:
        pair = coin["pair"]
        frames = load_mtf_frames(pair, config.KLINE_LIMIT)
        levels = calculate_advanced_trading_levels(
            frames["df_4h"], frames["df_1h"], frames["df_15m"]
        )
        if not levels:
            logger.info("levels None for %s", coin["symbol"])
            continue
        levels_map[coin["symbol"]] = levels
        coin["levels"] = levels
        px = bn.fetch_ticker_price(pair) or float(coin["price"])
        marks[coin["symbol"]] = px
        coin["price"] = px
        vol = (
            frames["df_15m"]["volume"]
            if "volume" in frames["df_15m"].columns
            else None
        )
        prepared.append(
            {
                **coin,
                "vs_btc": coin.get("vs_btc_24h") or coin.get("vs_btc_1h") or 0,
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
    # 挂单优先「推荐前三强」，不足再按 TOP10 补
    prefer_syms = [c["symbol"] for c in ranked.get("top3") or []]
    rest_syms = [
        c["symbol"]
        for c in ranked.get("top10") or []
        if c["symbol"] not in prefer_syms
    ]
    order_syms = prefer_syms + rest_syms
    coin_by_sym = {c["symbol"]: c for c in prepared}

    for sym in order_syms:
        coin = coin_by_sym.get(sym)
        if not coin:
            continue
        levels = coin["levels"]
        free_cash = max(0.0, equity - _reserved_notional())
        cash_backup = state["cash"]
        state["cash"] = min(float(cash_backup), free_cash)
        _place_or_refresh_pending(state, coin, levels, equity)
        state["cash"] = cash_backup

    snap = mark_equity(state, marks)
    state["last_screen"] = {
        "source": screen.get("source"),
        "btc_change_24h": screen.get("btc_change_24h"),
        "candidate_count": screen.get("candidate_count"),
        "twin_meta": screen.get("twin_meta"),
        "symbols": [c["symbol"] for c in ranked.get("top10") or top],
        "top3": [c["symbol"] for c in ranked.get("top3") or []],
        "top3_fallback": ranked.get("top3_fallback"),
    }
    state["last_rank"] = {
        "top3": ranked.get("top3") or [],
        "top10_scores": [
            {
                "symbol": x.get("symbol"),
                "total_score": x.get("total_score"),
                "hard_pass": x.get("hard_pass"),
            }
            for x in ranked.get("top10") or []
        ],
    }
    state["last_marks"] = marks
    store.save_state(state)

    summary = {
        "cycle": state["cycle"],
        "screen": screen,
        "ranked": ranked,
        "levels": levels_map,
        "snapshot": snap,
        "positions": state.get("positions") or {},
        "pending": state.get("pending") or {},
    }
    top3_str = ",".join(prefer_syms) or "-"
    line = (
        f"[{store.utc_now()}] cycle={state['cycle']} "
        f"top3=[{top3_str}] "
        f"top10={[c.get('symbol') for c in ranked.get('top10') or top]} "
        f"pos={list((state.get('positions') or {}).keys())} "
        f"pend={list((state.get('pending') or {}).keys())} "
        f"equity={snap['equity']:.2f}"
    )
    store.append_text(config.CYCLE_LOG_PATH, line)
    logger.info(line)
    return summary
