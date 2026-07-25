"""模拟下单与止损/止盈生命周期。"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from . import config as C
from . import journal

import audit_ledger as AL
from audit_ledger import pump_ledger

logger = logging.getLogger("pumpfun.execution")


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pos_metrics(pos: dict[str, Any], signal: dict[str, Any] | None = None) -> dict[str, Any]:
    src = signal or {}
    return {
        "panic_ratio": src.get("panic_ratio", pos.get("panic_ratio")),
        "ath_drop_pct": src.get("ath_drop_pct", pos.get("ath_drop_pct")),
        "whale_dump_pct": src.get("whale_dump_pct", pos.get("whale_dump_pct")),
        "spread_pct": src.get("spread_pct", pos.get("spread_pct")),
        "score": src.get("score", pos.get("score")),
        "age_minutes": src.get("age_minutes", pos.get("signal_age_minutes")),
        "slippage_pct": src.get("slippage_pct") or pos.get("slippage_pct") or 0.15,
        "fee_sol": pos.get("fees_sol"),
        "gas_sol": pos.get("gas_sol"),
        "slippage_sol": pos.get("slippage_sol"),
    }


class PaperBroker:
    """极度保守的纸面执行器（默认 DRY_RUN），含 DEX 手续费/Gas/滑点清算。"""

    def __init__(self) -> None:
        self.bankroll = C.BANKROLL_SOL
        self.cash = C.BANKROLL_SOL
        self.positions: dict[str, dict[str, Any]] = {}
        self.gross_realized = 0.0
        self.total_fees = 0.0
        self.total_slippage = 0.0
        self.total_gas = 0.0
        self.realized_pnl = 0.0  # 净已实现
        self.dry_run = C.DRY_RUN_DEFAULT
        self.last_audit: dict[str, Any] | None = None
        self._restore_account()

    def net_realized(self) -> float:
        return self.gross_realized - self.total_fees - self.total_slippage - self.total_gas

    # ---------- 账户持久化 ----------
    def _restore_account(self) -> None:
        """优先读账户文件；缺失时用历史成交/账本重建。"""
        try:
            if C.ACCOUNT_FILE.exists():
                saved = json.loads(C.ACCOUNT_FILE.read_text(encoding="utf-8"))
                self.gross_realized = float(saved.get("gross_realized_sol") or 0.0)
                self.total_fees = float(saved.get("total_fees_sol") or 0.0)
                self.total_slippage = float(saved.get("total_slippage_sol") or 0.0)
                self.total_gas = float(saved.get("total_gas_sol") or 0.0)
                self.realized_pnl = float(
                    saved.get("realized_pnl_sol")
                    if saved.get("realized_pnl_sol") is not None
                    else self.net_realized()
                )
                self.cash = float(saved.get("cash_sol") or (self.bankroll + self.realized_pnl))
                if not self.positions:
                    self.cash = self.bankroll + self.net_realized()
                    self.realized_pnl = self.net_realized()
                logger.info(
                    "账户已恢复 cash=%.6f net=%.6f fees=%.6f slip=%.6f gas=%.6f",
                    self.cash, self.realized_pnl, self.total_fees, self.total_slippage, self.total_gas,
                )
                return
            sums = pump_ledger.sum_costs()
            if sums["entry_count"]:
                self.gross_realized = sums["gross_realized"]
                self.total_fees = sums["fees"]
                self.total_slippage = sums["slippage"]
                self.total_gas = sums["gas"]
                self.realized_pnl = sums["net_realized"]
                self.cash = self.bankroll + self.realized_pnl
                logger.info("账户由复式账本重建 net=%.6f", self.realized_pnl)
                return
            self.realized_pnl = journal.lifetime_realized_pnl()
            self.gross_realized = self.realized_pnl
            self.cash = self.bankroll + self.realized_pnl
            if self.realized_pnl:
                logger.info("账户由历史成交重建 realized=%.6f", self.realized_pnl)
        except Exception:
            logger.exception("账户恢复失败，回落到初始本金")
            self.gross_realized = 0.0
            self.total_fees = 0.0
            self.total_slippage = 0.0
            self.total_gas = 0.0
            self.realized_pnl = 0.0
            self.cash = self.bankroll
        self._persist_account()

    def _persist_account(self) -> None:
        try:
            C.DATA_DIR.mkdir(parents=True, exist_ok=True)
            self.realized_pnl = self.net_realized()
            payload = {
                "bankroll_sol": round(self.bankroll, 8),
                "cash_sol": round(self.cash, 8),
                "gross_realized_sol": round(self.gross_realized, 8),
                "total_fees_sol": round(self.total_fees, 8),
                "total_slippage_sol": round(self.total_slippage, 8),
                "total_gas_sol": round(self.total_gas, 8),
                "realized_pnl_sol": round(self.realized_pnl, 8),
                "open_positions": len(self.positions),
                "updated_at": _utc(),
            }
            tmp = C.ACCOUNT_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(C.ACCOUNT_FILE)
        except Exception:
            logger.exception("账户持久化失败")

    def _charge_friction(self, *, amount_sol: float, side: str, pos: dict[str, Any], note: str) -> dict[str, float]:
        costs = AL.pump_trade_costs(amount_sol=amount_sol, side=side)
        fee, gas, slip = costs["fee_sol"], costs["gas_sol"], costs["slippage_sol"]
        self.cash -= fee + gas + slip
        self.total_fees += fee
        self.total_gas += gas
        self.total_slippage += slip
        pos["fees_sol"] = float(pos.get("fees_sol") or 0) + fee
        pos["gas_sol"] = float(pos.get("gas_sol") or 0) + gas
        pos["slippage_sol"] = float(pos.get("slippage_sol") or 0) + slip
        pump_ledger.append({"kind": "dex_fee", "amount": fee, "symbol": pos.get("symbol"), "position_id": pos.get("id"), "note": note, "meta": costs})
        pump_ledger.append({"kind": "gas", "amount": gas, "symbol": pos.get("symbol"), "position_id": pos.get("id"), "note": note, "meta": costs})
        pump_ledger.append({"kind": "slippage", "amount": slip, "symbol": pos.get("symbol"), "position_id": pos.get("id"), "note": note, "meta": costs})
        return costs

    def position_size_sol(self) -> float:
        size = self.bankroll * C.POSITION_PCT
        return max(C.MIN_POSITION_SOL, min(size, self.cash * 0.90))

    def open_long(self, signal: dict[str, Any], *, dry_run: bool | None = None) -> dict[str, Any] | None:
        dry = self.dry_run if dry_run is None else dry_run
        mint = signal["mint"]
        if mint in self.positions:
            return None
        if len(self.positions) >= C.MAX_OPEN_POSITIONS:
            return None
        mid = float(signal["price"])
        if mid <= 0:
            return None
        sol = self.position_size_sol()
        preview = AL.pump_trade_costs(amount_sol=sol, side="buy")
        if sol + preview["total_friction_sol"] > self.cash:
            return None
        qty = sol / mid  # mid 口径数量
        costs = preview
        fill_px = AL.pump_fill_price(mid, side="buy", slip_pct=costs["slippage_pct"])
        pos = {
            "id": str(uuid.uuid4())[:8],
            "mint": mint,
            "symbol": signal.get("symbol") or mint[:6],
            "entry": mid,
            "qty": qty,
            "qty_left": qty,
            "sol_spent": sol,
            "opened_at": time.time(),
            "opened_at_iso": _utc(),
            "peak": mid,
            "tp1_done": False,
            "trail_line": None,
            "dry_run": dry,
            "status": "open",
            "score": signal.get("score"),
            "ath_drop_pct": signal.get("ath_drop_pct"),
            "panic_ratio": signal.get("panic_ratio"),
            "whale_dump_pct": signal.get("whale_dump_pct"),
            "spread_pct": signal.get("spread_pct"),
            "signal_age_minutes": signal.get("age_minutes"),
            "slippage_pct": costs["slippage_pct"],
            "fees_sol": 0.0,
            "gas_sol": 0.0,
            "slippage_sol": 0.0,
            "fill_entry": fill_px,
        }
        self.cash -= sol
        self.positions[mint] = pos
        self._charge_friction(amount_sol=sol, side="buy", pos=pos, note="buy")
        self._persist_account()
        trade = journal.record_trade(
            action="buy",
            mint=mint,
            symbol=pos["symbol"],
            amount_sol=sol,
            price=mid,
            pnl_sol=None,
            pnl_percent=None,
            exit_reason="",
            dry_run=dry,
            metrics=_pos_metrics(pos, signal),
            position_id=pos["id"],
        )
        trade["fee_sol"] = pos["fees_sol"]
        trade["gas_sol"] = pos["gas_sol"]
        trade["slippage_sol"] = pos["slippage_sol"]
        trade["fill_price"] = fill_px
        logger.info(
            "%s OPEN %s @%.8g sol=%.4f fee=%.6f gas=%.6f slip=%.6f",
            "[DRY]" if dry else "[LIVE]", pos["symbol"], mid, sol,
            pos["fees_sol"], pos["gas_sol"], pos["slippage_sol"],
        )
        pos["last_trade"] = trade
        return pos

    def mark(self, mint: str, price: float) -> None:
        pos = self.positions.get(mint)
        if not pos or price <= 0:
            return
        pos["mark"] = price
        pos["peak"] = max(float(pos.get("peak") or 0), price)
        pos["trail_line"] = float(pos["peak"]) * (1.0 - C.TRAIL_DRAWDOWN)
        entry = float(pos["entry"])
        pos["pnl_pct"] = (price - entry) / entry if entry else 0.0

    def _close_partial(
        self, pos: dict[str, Any], ratio: float, price: float, reason: str
    ) -> dict[str, Any]:
        ratio = max(0.0, min(1.0, ratio))
        qty = float(pos["qty_left"]) * ratio
        if qty <= 0:
            return {}
        mid = float(price)
        entry = float(pos["entry"])
        # 毛盈亏：mid 口径
        proceeds = qty * mid
        cost = qty * entry
        gross = proceeds - cost
        pnl_pct = ((mid - entry) / entry * 100.0) if entry > 0 else 0.0
        pos["qty_left"] = float(pos["qty_left"]) - qty

        costs = self._charge_friction(amount_sol=proceeds, side="sell", pos=pos, note=reason)
        fill_px = AL.pump_fill_price(mid, side="sell", slip_pct=costs["slippage_pct"])

        self.cash += proceeds
        self.gross_realized += gross
        pump_ledger.append({
            "kind": "gross_pnl",
            "amount": gross,
            "symbol": pos["symbol"],
            "position_id": pos.get("id"),
            "note": reason,
            "meta": {"mid": mid, "entry": entry, "qty": qty},
        })
        self.realized_pnl = self.net_realized()
        self._persist_account()
        net = gross - costs["fee_sol"] - costs["gas_sol"] - costs["slippage_sol"]
        logger.info(
            "SETTLE %s %s gross=%+.6f fee=%.6f gas=%.6f slip=%.6f net=%+.6f equity=%.6f",
            reason, pos["symbol"], gross, costs["fee_sol"], costs["gas_sol"],
            costs["slippage_sol"], net, self.equity(),
        )
        trade = journal.record_trade(
            action=reason,
            mint=pos["mint"],
            symbol=pos["symbol"],
            amount_sol=proceeds,
            price=mid,
            pnl_sol=net,
            pnl_percent=pnl_pct,
            dry_run=bool(pos.get("dry_run")),
            metrics=_pos_metrics(pos),
            position_id=pos.get("id"),
        )
        trade["gross_pnl_sol"] = round(gross, 8)
        trade["fee_sol"] = costs["fee_sol"]
        trade["gas_sol"] = costs["gas_sol"]
        trade["slippage_sol"] = costs["slippage_sol"]
        trade["fill_price"] = fill_px
        self.run_audit(auto_correct=True)
        return trade

    def manage(self, price_map: dict[str, float]) -> list[dict[str, Any]]:
        """对所有持仓执行：TP1 / 回撤止盈 / 时间止损。"""
        events: list[dict[str, Any]] = []
        now = time.time()
        for mint, pos in list(self.positions.items()):
            px = price_map.get(mint)
            if px is None:
                px = float(pos.get("mark") or pos["entry"])
            self.mark(mint, px)
            age_m = (now - float(pos["opened_at"])) / 60.0
            entry = float(pos["entry"])
            pnl_pct = (px - entry) / entry if entry else 0.0

            if not pos.get("tp1_done") and pnl_pct >= C.TP1_PCT:
                trade = self._close_partial(pos, C.TP1_SELL_RATIO, px, "tp1")
                pos["tp1_done"] = True
                pos["peak"] = px
                pos["trail_line"] = px * (1.0 - C.TRAIL_DRAWDOWN)
                events.append(
                    {"type": "tp1", "symbol": pos["symbol"], "mint": mint, "price": px, "pnl_pct": pnl_pct, "trade": trade}
                )
                logger.info("TP1 %s @%.8g (+%.1f%%)", pos["symbol"], px, pnl_pct * 100)

            if pos.get("tp1_done") and pos.get("trail_line") is not None:
                if px <= float(pos["trail_line"]):
                    trade = self._close_partial(pos, 1.0, px, "trail_stop")
                    events.append({"type": "trail_stop", "symbol": pos["symbol"], "mint": mint, "price": px, "trade": trade})
                    logger.info("TRAIL %s @%.8g line=%.8g", pos["symbol"], px, pos["trail_line"])
                    self.positions.pop(mint, None)
                    continue

            if age_m >= C.TIME_STOP_MINUTES:
                trade = self._close_partial(pos, 1.0, px, "time_stop")
                events.append({"type": "time_stop", "symbol": pos["symbol"], "mint": mint, "price": px, "age_m": age_m, "trade": trade})
                logger.info("TIME_STOP %s after %.1fm", pos["symbol"], age_m)
                self.positions.pop(mint, None)
                continue

            if float(pos.get("qty_left") or 0) <= 1e-18:
                self.positions.pop(mint, None)

        return events

    def snapshot_positions(self) -> list[dict[str, Any]]:
        rows = []
        for pos in self.positions.values():
            mark = float(pos.get("mark") or pos["entry"])
            entry = float(pos["entry"])
            pnl_pct = ((mark - entry) / entry * 100.0) if entry > 0 else 0.0
            rows.append(
                {
                    "id": pos["id"],
                    "mint": pos["mint"],
                    "symbol": pos["symbol"],
                    "entry": entry,
                    "mark": mark,
                    "current_price": mark,
                    "entry_price": entry,
                    "pnl_pct": round(pnl_pct, 2),
                    "tp1_done": bool(pos.get("tp1_done")),
                    "trail_line": pos.get("trail_line"),
                    "qty_left_ratio": round(
                        float(pos["qty_left"]) / float(pos["qty"]), 3
                    )
                    if float(pos["qty"])
                    else 0,
                    "age_minutes": round((time.time() - float(pos["opened_at"])) / 60.0, 1),
                    "dry_run": pos.get("dry_run"),
                    "sol_spent": pos.get("sol_spent"),
                    "fees_sol": pos.get("fees_sol"),
                    "gas_sol": pos.get("gas_sol"),
                    "slippage_sol": pos.get("slippage_sol"),
                }
            )
        return rows

    def unrealized_pnl(self) -> float:
        total = 0.0
        for pos in self.positions.values():
            mark = float(pos.get("mark") or pos["entry"])
            entry = float(pos["entry"])
            total += (mark - entry) * float(pos["qty_left"])
        return total

    def position_value(self) -> float:
        return sum(
            float(pos["qty_left"]) * float(pos.get("mark") or pos["entry"])
            for pos in self.positions.values()
        )

    def equity(self) -> float:
        """运营权益 = 现金 + 在仓市值。"""
        return self.cash + self.position_value()

    def equity_from_ledger(self) -> float:
        return AL.expected_equity(
            initial=self.bankroll,
            gross_realized=self.gross_realized,
            fees=self.total_fees,
            slippage=self.total_slippage,
            funding=0.0,
            unrealized=self.unrealized_pnl(),
            gas=self.total_gas,
        )

    def run_audit(self, *, auto_correct: bool = True) -> dict[str, Any]:
        result = AL.run_audit_check(
            pump_ledger,
            initial=self.bankroll,
            displayed_equity=self.equity(),
            displayed_realized_net=self.net_realized(),
            unrealized=self.unrealized_pnl(),
            auto_correct=auto_correct,
        )
        if (not result["ok"]) and auto_correct and result.get("correction"):
            sums = pump_ledger.sum_costs()
            self.gross_realized = sums["gross_realized"]
            self.total_fees = sums["fees"]
            self.total_slippage = sums["slippage"]
            self.total_gas = sums["gas"]
            self.realized_pnl = sums["net_realized"]
            # 现金 = 本金 + 净实现 - 在仓成本（sol_spent * qty_left/qty）
            locked = 0.0
            for pos in self.positions.values():
                q = float(pos["qty"]) or 1.0
                locked += float(pos["sol_spent"]) * (float(pos["qty_left"]) / q)
            self.cash = self.bankroll + self.net_realized() - locked
            self._persist_account()
            result["corrected"] = True
            result["equity_after"] = self.equity()
            logger.warning("[AUDIT] Pump 账目已按复式账本修正 equity=%.6f", self.equity())
        self.last_audit = result
        return result

    def audit_report_24h(self) -> dict[str, Any]:
        sums = pump_ledger.sum_costs(hours=24.0)
        return AL.build_24h_audit_report(
            pump_ledger,
            initial=self.bankroll,
            displayed_equity=self.equity(),
            unrealized=self.unrealized_pnl(),
            extra={
                "total_fees_sol": sums["fees"],
                "total_slippage_sol": sums["slippage"],
                "total_gas_sol": sums["gas"],
            },
        )
