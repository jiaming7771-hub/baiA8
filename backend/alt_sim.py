"""雷达 #1 极速狙击模拟仓：可配置总资金 + 单标的市价快成交 + 24h 复盘。

核心规则：
- 每次雷达刷新只交易综合评分第一名（不再同时挂前三强）
- 第一名切换时评估轮换：平掉旧仓后立即狙击新 #1
- 现价贴盘 / 极小滑点市价成交，杜绝挂单长期不成交
- 仓位仍按策略 30% 试错 + 70% 补仓；T1 立即市价成交，T2 贴近现价或下探时秒补
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import audit_ledger as AL
from audit_ledger import cex_ledger

logger = logging.getLogger("alt_sim")

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "alt_sim_data"
TRADES_FILE = DATA_DIR / "alt_sim_trades.jsonl"
ACCOUNT_FILE = DATA_DIR / "alt_sim_account.json"

# ---------- 资金与杠杆（env / config 可覆盖） ----------
BANKROLL_USD = float(os.getenv("ALT_SIM_BANKROLL_USD", "1000"))  # 模拟总资金
# 单笔保证金：优先固定 U；若设 ALT_SIM_POSITION_PCT（如 0.1=10%）则按总资金百分比
_POSITION_PCT = os.getenv("ALT_SIM_POSITION_PCT", "").strip()
POSITION_PCT = float(_POSITION_PCT) if _POSITION_PCT else None
MARGIN_USD = float(os.getenv("ALT_SIM_MARGIN_USD", "100"))  # 固定保证金（POSITION_PCT 未设时生效）
LEVERAGE = max(2.0, min(10.0, float(os.getenv("ALT_SIM_LEVERAGE", "10"))))
TRANCHE1_RATIO = 0.30
TRANCHE2_RATIO = 0.70
MAX_OPEN = 1  # 极速狙击：永远只持有 1 个标的
T2_CHASE_SEC = float(os.getenv("ALT_SIM_T2_CHASE_SEC", "45"))  # T2 未成交则追价市价补齐
ROTATE_MIN_SCORE_EDGE = float(os.getenv("ALT_SIM_ROTATE_SCORE_EDGE", "0.5"))  # 换仓所需评分优势
COOLDOWN_MIN = float(os.getenv("ALT_SIM_COOLDOWN_MIN", "5"))  # 同币平仓后短冷却
# 兼容旧配置名（实际滑点由 audit_ledger.cex_slippage_bps 决定）
SLIPPAGE_BPS = float(os.getenv("ALT_SIM_SLIPPAGE_BPS", str(AL.CEX_SLIP_BPS_MIN)))

ACTION_LABELS = {
    "open_t1": "狙击#1·首仓30%市价",
    "open_t2": "第二仓70%补仓",
    "open_t2_chase": "第二仓追价补齐",
    "take_profit": "止盈平仓",
    "hard_stop": "硬止损",
    "liquidation": "强平爆仓",
    "rotate": "换仓·平旧#1",
    "funding": "资金费率结算",
    "cancel": "计划取消",
}

EXIT_ACTIONS = ("take_profit", "hard_stop", "liquidation", "rotate")

_lock = threading.Lock()


def position_margin() -> float:
    if POSITION_PCT is not None and POSITION_PCT > 0:
        return max(1.0, BANKROLL_USD * POSITION_PCT)
    return max(1.0, MARGIN_USD)


def _utc_iso(ts: float | None = None) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc) if ts else datetime.now(timezone.utc)
    return dt.isoformat()


def _append_trade(row: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with TRADES_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _parse_levels(pick: dict[str, Any]) -> tuple[float, float, float, float] | None:
    t1 = pick.get("tranche_1_price") or pick.get("entry")
    t2 = pick.get("tranche_2_price") or pick.get("entry")
    stop = pick.get("stop_loss") or pick.get("order_stop")
    take = pick.get("take_profit") or pick.get("order_take")
    try:
        t1, t2 = float(t1), float(t2)
        stop, take = float(stop), float(take)
    except (TypeError, ValueError):
        return None
    if not (0 < stop < take and t1 > 0 and t2 > 0):
        return None
    # 允许 t2≈t1（市价快成交场景），但止损仍须低于入场参考
    if stop >= min(t1, t2):
        return None
    return t1, t2, stop, take


class AltTop3Simulator:
    """只狙击雷达 #1 的纸面撮合引擎。"""

    def __init__(self) -> None:
        self.positions: dict[str, dict[str, Any]] = {}
        self.cooldown_until: dict[str, float] = {}
        self.cash = BANKROLL_USD
        self.gross_realized_usd = 0.0  # 价差毛盈亏（不含费用）
        self.total_fees_usd = 0.0
        self.total_slippage_usd = 0.0
        self.total_funding_usd = 0.0
        self.realized_pnl_usd = 0.0  # 净已实现 = gross - fees - slip - funding
        self.current_target: str | None = None
        self.updated_at: str | None = None
        self.last_audit: dict[str, Any] | None = None
        self._restore_account()

    def net_realized(self) -> float:
        return (
            self.gross_realized_usd
            - self.total_fees_usd
            - self.total_slippage_usd
            - self.total_funding_usd
        )

    # ---------- 账户 ----------
    def _restore_account(self) -> None:
        try:
            if ACCOUNT_FILE.exists():
                saved = json.loads(ACCOUNT_FILE.read_text(encoding="utf-8"))
                self.gross_realized_usd = float(saved.get("gross_realized_usd") or 0.0)
                self.total_fees_usd = float(saved.get("total_fees_usd") or 0.0)
                self.total_slippage_usd = float(saved.get("total_slippage_usd") or 0.0)
                self.total_funding_usd = float(saved.get("total_funding_usd") or 0.0)
                self.realized_pnl_usd = float(
                    saved.get("realized_pnl_usd")
                    if saved.get("realized_pnl_usd") is not None
                    else self.net_realized()
                )
                self.cash = float(saved.get("cash_usd") or (BANKROLL_USD + self.realized_pnl_usd))
                if not self.positions:
                    self.cash = BANKROLL_USD + self.net_realized()
                    self.realized_pnl_usd = self.net_realized()
                logger.info(
                    "ALT_SIM 账户恢复 cash=%.2f net=%+.2f fees=%.4f slip=%.4f funding=%.4f",
                    self.cash, self.realized_pnl_usd,
                    self.total_fees_usd, self.total_slippage_usd, self.total_funding_usd,
                )
                return
            # 无账户文件：从复式账本重建
            sums = cex_ledger.sum_costs()
            self.gross_realized_usd = sums["gross_realized"]
            self.total_fees_usd = sums["fees"]
            self.total_slippage_usd = sums["slippage"]
            self.total_funding_usd = sums["funding"]
            self.realized_pnl_usd = sums["net_realized"]
            self.cash = BANKROLL_USD + self.realized_pnl_usd
        except Exception:
            logger.exception("ALT_SIM 账户恢复失败")
            self.gross_realized_usd = 0.0
            self.total_fees_usd = 0.0
            self.total_slippage_usd = 0.0
            self.total_funding_usd = 0.0
            self.realized_pnl_usd = 0.0
            self.cash = BANKROLL_USD
        self._persist_account()

    def _persist_account(self) -> None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            self.realized_pnl_usd = self.net_realized()
            payload = {
                "bankroll_usd": round(BANKROLL_USD, 4),
                "cash_usd": round(self.cash, 4),
                "gross_realized_usd": round(self.gross_realized_usd, 6),
                "total_fees_usd": round(self.total_fees_usd, 6),
                "total_slippage_usd": round(self.total_slippage_usd, 6),
                "total_funding_usd": round(self.total_funding_usd, 6),
                "realized_pnl_usd": round(self.realized_pnl_usd, 6),
                "open_positions": len(self.positions),
                "current_target": self.current_target,
                "updated_at": _utc_iso(),
            }
            tmp = ACCOUNT_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(ACCOUNT_FILE)
        except Exception:
            logger.exception("ALT_SIM 账户落盘失败")

    def unrealized_pnl(self) -> float:
        total = 0.0
        for pos in self.positions.values():
            if pos["status"] != "open" or pos["filled_notional"] <= 0:
                continue
            p = pos.get("mark")
            avg = float(pos.get("avg_entry") or 0)  # mid 口径入场
            if not p or avg <= 0:
                continue
            total += pos["filled_notional"] * (float(p) / avg - 1.0)
        return total

    def locked_margin(self) -> float:
        return sum(float(p.get("margin_usd") or 0) for p in self.positions.values() if p.get("status") == "open")

    def equity(self) -> float:
        """运营权益 = 现金 + 占用保证金 + 浮动（应与复式恒等式一致）。"""
        return self.cash + self.locked_margin() + self.unrealized_pnl()

    def equity_from_ledger(self) -> float:
        return AL.expected_equity(
            initial=BANKROLL_USD,
            gross_realized=self.gross_realized_usd,
            fees=self.total_fees_usd,
            slippage=self.total_slippage_usd,
            funding=self.total_funding_usd,
            unrealized=self.unrealized_pnl(),
        )

    # ---------- 建仓 / 轮换 ----------
    def on_radar_top3(
        self,
        top3: list[dict[str, Any]],
        *,
        live_prices: dict[str, float] | None = None,
    ) -> list[dict[str, Any]]:
        """只取 #1；必要时平旧换新，并以市价极速开仓。"""
        events: list[dict[str, Any]] = []
        if not top3:
            return events
        pick = top3[0]
        sym = pick.get("symbol")
        if not sym:
            return events
        levels = _parse_levels(pick)
        if not levels:
            logger.warning("ALT_SIM skip %s: invalid levels", sym)
            return events

        mark = None
        if live_prices and live_prices.get(sym):
            mark = float(live_prices[sym])
        elif pick.get("price"):
            try:
                mark = float(pick["price"])
            except (TypeError, ValueError):
                mark = None
        if not mark or mark <= 0:
            logger.warning("ALT_SIM skip %s: no live mark", sym)
            return events

        score = float(pick.get("total_score") or pick.get("composite_score") or 0)
        now = time.time()
        t1, t2, stop, take = levels

        with _lock:
            # 已持有同一 #1：刷新点位，必要时追价补 T2
            if sym in self.positions:
                pos = self.positions[sym]
                pos["stop_price"] = stop
                pos["take_price"] = take
                pos["t1_price"] = t1
                pos["t2_price"] = t2
                pos["target_score"] = score
                pos["mark"] = mark
                self.current_target = sym
                if not pos.get("t2_filled") and pos.get("t1_filled"):
                    events.extend(self._maybe_fill_t2(pos, mark, now, force_chase=False))
                return events

            # 持有旧标的 → 评估轮换
            if self.positions:
                old_sym, old_pos = next(iter(self.positions.items()))
                old_score = float(old_pos.get("target_score") or 0)
                avg = float(old_pos.get("avg_entry") or 0)
                old_mark = float(old_pos.get("mark") or avg or 0)
                unreal = 0.0
                if avg > 0 and old_mark > 0 and old_pos.get("filled_notional"):
                    unreal = old_pos["filled_notional"] * (old_mark / avg - 1.0)
                # 浮盈且新 #1 评分优势不足 → 继续持有；否则轮换到新第一名
                should_rotate = True
                if unreal > 0 and score < old_score + ROTATE_MIN_SCORE_EDGE:
                    should_rotate = False
                    logger.info(
                        "ALT_SIM keep %s (浮盈中) 新#1=%s score=%.1f vs %.1f",
                        old_sym, sym, score, old_score,
                    )
                if should_rotate:
                    if old_pos.get("status") == "open" and old_pos.get("filled_notional"):
                        events.append(self._close(old_pos, old_mark, "rotate"))
                    else:
                        self._journal(old_pos, "cancel")
                        self.positions.pop(old_sym, None)
                    logger.info("ALT_SIM rotate %s → %s", old_sym, sym)
                else:
                    return events

            if self.cooldown_until.get(sym, 0) > now:
                return events
            if len(self.positions) >= MAX_OPEN:
                return events

            margin = position_margin()
            # 预估首仓摩擦，确保现金够用
            preview = AL.cex_trade_costs(notional_usd=margin * LEVERAGE * TRANCHE1_RATIO, side="buy")
            need = margin + preview["total_friction_usd"]
            if self.cash < need:
                logger.warning("ALT_SIM 资金不足 cash=%.2f need=%.2f", self.cash, need)
                return events

            notional = margin * LEVERAGE
            # 止损/止盈相对现价（mid）校验
            stop_eff = stop if stop < mark else mark * 0.985
            take_eff = take if take > mark else mark * 1.02

            pos = {
                "id": str(uuid.uuid4())[:8],
                "symbol": sym,
                "leverage": LEVERAGE,
                "margin_usd": margin,
                "notional_usd": notional,
                "t1_price": t1,
                "t2_price": t2,
                "stop_price": stop_eff,
                "take_price": take_eff,
                "t1_filled": False,
                "t2_filled": False,
                "filled_notional": 0.0,
                "avg_entry": 0.0,  # mid 口径，用于毛盈亏
                "mark": mark,
                "status": "pending",
                "target_score": score,
                "created_at": now,
                "created_at_iso": _utc_iso(now),
                "t1_filled_at": None,
                "last_funding_at": now,
                "fees_usd": 0.0,
                "slippage_usd": 0.0,
            }
            self.cash -= margin
            self.positions[sym] = pos
            self.current_target = sym

            # 极速：T1 按 mid 记账 + 显式扣费/滑点
            events.append(self._fill(pos, TRANCHE1_RATIO, mark, "open_t1"))
            pos["t1_filled"] = True
            pos["t1_filled_at"] = now
            if mark <= t2 * 1.001:
                events.append(self._fill(pos, TRANCHE2_RATIO, min(mark, t2), "open_t2"))
                pos["t2_filled"] = True

            self._persist_account()
            logger.info(
                "ALT_SIM sniper #1 %s mark=%.8g stop=%.8g take=%.8g margin=%.0f lev=%.0fx",
                sym, mark, stop_eff, take_eff, margin, LEVERAGE,
            )
        self.updated_at = _utc_iso()
        return events

    def _maybe_fill_t2(
        self, pos: dict[str, Any], mark: float, now: float, *, force_chase: bool
    ) -> list[dict[str, Any]]:
        if pos.get("t2_filled") or not pos.get("t1_filled"):
            return []
        t2 = float(pos["t2_price"])
        # 价格下探到补仓价，或超时追价
        chase_due = False
        t1_at = pos.get("t1_filled_at")
        if t1_at and (now - float(t1_at)) >= T2_CHASE_SEC:
            chase_due = True
        if mark <= t2 * 1.001:
            row = self._fill(pos, TRANCHE2_RATIO, min(mark, t2), "open_t2")
            pos["t2_filled"] = True
            return [row]
        if force_chase or chase_due:
            row = self._fill(pos, TRANCHE2_RATIO, mark, "open_t2_chase")
            pos["t2_filled"] = True
            return [row]
        return []

    # ---------- 撮合 ----------
    def _charge_friction(
        self, pos: dict[str, Any], *, notional: float, side: str, note: str
    ) -> dict[str, float]:
        costs = AL.cex_trade_costs(notional_usd=notional, side=side, is_taker=True)
        fee = costs["fee_usd"]
        slip = costs["slippage_usd"]
        self.cash -= fee + slip
        self.total_fees_usd += fee
        self.total_slippage_usd += slip
        pos["fees_usd"] = float(pos.get("fees_usd") or 0) + fee
        pos["slippage_usd"] = float(pos.get("slippage_usd") or 0) + slip
        cex_ledger.append({
            "kind": "fee",
            "amount": fee,
            "symbol": pos["symbol"],
            "position_id": pos["id"],
            "note": note,
            "meta": costs,
        })
        cex_ledger.append({
            "kind": "slippage",
            "amount": slip,
            "symbol": pos["symbol"],
            "position_id": pos["id"],
            "note": note,
            "meta": costs,
        })
        return costs

    def _fill(self, pos: dict[str, Any], ratio: float, mid: float, action: str) -> dict[str, Any]:
        """按 mid 记账开仓，并扣除 Taker 手续费 + 盘口滑点。"""
        add_notional = pos["notional_usd"] * ratio
        costs = self._charge_friction(pos, notional=add_notional, side="buy", note=action)
        fill_px = AL.cex_fill_price(mid, side="buy", slip_bps=costs["slippage_bps"])
        prev = pos["filled_notional"]
        # 毛盈亏用 mid 均价，避免与显式滑点双计
        pos["avg_entry"] = (
            (pos["avg_entry"] * prev + float(mid) * add_notional) / (prev + add_notional)
            if prev + add_notional > 0
            else float(mid)
        )
        pos["filled_notional"] = prev + add_notional
        pos["status"] = "open"
        return self._journal(
            pos,
            action,
            entry_price=float(mid),
            fill_price=fill_px,
            position_size_usd=add_notional,
            fee_usd=costs["fee_usd"],
            slippage_usd=costs["slippage_usd"],
        )

    def _close(self, pos: dict[str, Any], mid: float, reason: str) -> dict[str, Any]:
        avg = float(pos["avg_entry"]) or float(mid)
        filled = float(pos["filled_notional"])
        margin = float(pos["margin_usd"])
        # 毛盈亏（mid→mid）
        if reason == "liquidation":
            gross = -margin
        else:
            gross = filled * (float(mid) / avg - 1.0) if avg > 0 and filled > 0 else 0.0

        costs = self._charge_friction(pos, notional=filled, side="sell", note=reason) if filled > 0 else {
            "fee_usd": 0.0, "slippage_usd": 0.0, "slippage_bps": 0.0,
        }
        fill_px = AL.cex_fill_price(mid, side="sell", slip_bps=costs.get("slippage_bps", 0)) if filled > 0 else mid

        self.gross_realized_usd += gross
        cex_ledger.append({
            "kind": "gross_pnl",
            "amount": gross,
            "symbol": pos["symbol"],
            "position_id": pos["id"],
            "note": reason,
            "meta": {"mid": mid, "avg_entry": avg, "filled": filled},
        })

        # 保证金 + 毛盈亏回流（费用/滑点已在 _charge_friction 扣过）
        self.cash += margin + gross
        self.realized_pnl_usd = self.net_realized()

        row = self._journal(
            pos,
            reason,
            entry_price=avg,
            exit_price=float(mid),
            fill_price=fill_px,
            pnl_usd=gross - costs["fee_usd"] - costs["slippage_usd"],  # 净盈亏展示
            gross_pnl_usd=gross,
            position_size_usd=filled,
            fee_usd=costs["fee_usd"],
            slippage_usd=costs["slippage_usd"],
        )
        logger.info(
            "ALT_SIM SETTLE %s %s gross=%+.4f fee=%.4f slip=%.4f net=%+.4f equity=%.2f",
            reason, pos["symbol"], gross, costs["fee_usd"], costs["slippage_usd"],
            row.get("pnl_usd"), self.equity(),
        )
        self.cooldown_until[pos["symbol"]] = time.time() + COOLDOWN_MIN * 60
        self.positions.pop(pos["symbol"], None)
        if self.current_target == pos["symbol"]:
            self.current_target = None
        self._persist_account()
        self.run_audit(auto_correct=True)
        return row

    def _apply_funding(self, pos: dict[str, Any], now: float) -> dict[str, Any] | None:
        last = float(pos.get("last_funding_at") or pos.get("created_at") or now)
        if now - last < AL.CEX_FUNDING_INTERVAL_SEC:
            return None
        if pos["status"] != "open" or pos["filled_notional"] <= 0:
            return None
        fee = AL.cex_funding_fee(notional_usd=pos["filled_notional"])
        self.cash -= fee
        self.total_funding_usd += fee
        pos["last_funding_at"] = now
        cex_ledger.append({
            "kind": "funding",
            "amount": fee,
            "symbol": pos["symbol"],
            "position_id": pos["id"],
            "note": "funding_settlement",
            "meta": {"notional": pos["filled_notional"], "interval_sec": AL.CEX_FUNDING_INTERVAL_SEC},
        })
        self._persist_account()
        logger.info("ALT_SIM FUNDING %s -%.6fU notional=%.2f", pos["symbol"], fee, pos["filled_notional"])
        return self._journal(
            pos, "funding",
            entry_price=pos.get("avg_entry"),
            position_size_usd=pos["filled_notional"],
            pnl_usd=-fee,
            fee_usd=0.0,
            slippage_usd=0.0,
            funding_usd=fee,
        )

    def _journal(
        self,
        pos: dict[str, Any],
        action: str,
        *,
        entry_price: float | None = None,
        exit_price: float | None = None,
        fill_price: float | None = None,
        pnl_usd: float | None = None,
        gross_pnl_usd: float | None = None,
        position_size_usd: float | None = None,
        fee_usd: float | None = None,
        slippage_usd: float | None = None,
        funding_usd: float | None = None,
    ) -> dict[str, Any]:
        avg = float(pos.get("avg_entry") or 0)
        margin = float(pos.get("margin_usd") or 0)
        pnl_pct = None
        if pnl_usd is not None and margin:
            pnl_pct = pnl_usd / margin * 100.0
        row = {
            "timestamp": _utc_iso(),
            "symbol": pos["symbol"],
            "position_id": pos["id"],
            "action": action,
            "action_label": ACTION_LABELS.get(action, action),
            "leverage": pos["leverage"],
            "margin_usd": pos["margin_usd"],
            "position_size_usd": round(
                position_size_usd if position_size_usd is not None else pos["filled_notional"], 4
            ),
            "entry_price": entry_price if entry_price is not None else (avg or None),
            "exit_price": exit_price,
            "fill_price": fill_price,
            "pnl_usd": None if pnl_usd is None else round(pnl_usd, 4),
            "gross_pnl_usd": None if gross_pnl_usd is None else round(gross_pnl_usd, 4),
            "fee_usd": round(fee_usd or 0.0, 6),
            "slippage_usd": round(slippage_usd or 0.0, 6),
            "funding_usd": round(funding_usd or 0.0, 6),
            "pnl_pct_on_margin": None if pnl_pct is None else round(pnl_pct, 2),
            "exit_reason": ACTION_LABELS.get(action, "") if action in EXIT_ACTIONS or action == "cancel" else "",
            "sniper_rank": 1,
            "target_score": pos.get("target_score"),
        }
        _append_trade(row)
        logger.info(
            "ALT_SIM %s %s entry=%s exit=%s pnl=%s fee=%s slip=%s",
            action, pos["symbol"], row["entry_price"], exit_price, row["pnl_usd"],
            row["fee_usd"], row["slippage_usd"],
        )
        return row

    def on_prices(self, prices: dict[str, float]) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        now = time.time()
        with _lock:
            for sym, pos in list(self.positions.items()):
                p = prices.get(sym)
                if p is None or p <= 0:
                    continue
                pos["mark"] = p

                if not pos.get("t2_filled") and pos.get("t1_filled"):
                    events.extend(self._maybe_fill_t2(pos, p, now, force_chase=False))

                fund_ev = self._apply_funding(pos, now)
                if fund_ev:
                    events.append(fund_ev)

                if pos["status"] != "open" or pos["filled_notional"] <= 0:
                    continue

                avg = float(pos["avg_entry"])
                unreal = pos["filled_notional"] * (p / avg - 1.0)

                if unreal <= -float(pos["margin_usd"]):
                    liq_price = avg * (1.0 - float(pos["margin_usd"]) / pos["filled_notional"])
                    events.append(self._close(pos, liq_price, "liquidation"))
                    continue
                if p <= pos["stop_price"]:
                    events.append(self._close(pos, float(pos["stop_price"]), "hard_stop"))
                    continue
                if p >= pos["take_price"]:
                    events.append(self._close(pos, float(pos["take_price"]), "take_profit"))
                    continue
        if events:
            self.updated_at = _utc_iso()
        return events

    def run_audit(self, *, auto_correct: bool = True) -> dict[str, Any]:
        result = AL.run_audit_check(
            cex_ledger,
            initial=BANKROLL_USD,
            displayed_equity=self.equity(),
            displayed_realized_net=self.net_realized(),
            unrealized=self.unrealized_pnl(),
            auto_correct=auto_correct,
        )
        if (not result["ok"]) and auto_correct and result.get("correction"):
            corr = result["correction"]
            # 以账本为准修正累计器（持仓占用保证金保持）
            sums = cex_ledger.sum_costs()
            self.gross_realized_usd = sums["gross_realized"]
            self.total_fees_usd = sums["fees"]
            self.total_slippage_usd = sums["slippage"]
            self.total_funding_usd = sums["funding"]
            self.realized_pnl_usd = sums["net_realized"]
            self.cash = BANKROLL_USD + self.net_realized() - self.locked_margin()
            self._persist_account()
            result["corrected"] = True
            result["equity_after"] = self.equity()
            logger.warning("[AUDIT] ALT_SIM 账目已按复式账本修正 equity=%.4f", self.equity())
        self.last_audit = result
        return result

    def audit_report_24h(self) -> dict[str, Any]:
        stats = self.stats_24h()
        return AL.build_24h_audit_report(
            cex_ledger,
            initial=BANKROLL_USD,
            displayed_equity=self.equity(),
            unrealized=self.unrealized_pnl(),
            win_rate=stats.get("win_rate"),
            total_trades=stats.get("total_trades"),
            extra={
                "current_target": self.current_target,
                "leverage": LEVERAGE,
                "margin_usd": position_margin(),
            },
        )

    # ---------- 查询 ----------
    def open_positions(self) -> list[dict[str, Any]]:
        rows = []
        with _lock:
            for pos in self.positions.values():
                p = pos.get("mark")
                avg = float(pos.get("avg_entry") or 0)
                unreal = None
                if pos["status"] == "open" and p and avg > 0:
                    unreal = pos["filled_notional"] * (float(p) / avg - 1.0)
                rows.append(
                    {
                        "id": pos["id"],
                        "symbol": pos["symbol"],
                        "status": pos["status"],
                        "sniper_rank": 1,
                        "target_score": pos.get("target_score"),
                        "leverage": pos["leverage"],
                        "margin_usd": pos["margin_usd"],
                        "notional_usd": pos["notional_usd"],
                        "filled_notional_usd": round(pos["filled_notional"], 4),
                        "t1_price": pos["t1_price"],
                        "t2_price": pos["t2_price"],
                        "stop_price": pos["stop_price"],
                        "take_price": pos["take_price"],
                        "t1_filled": pos["t1_filled"],
                        "t2_filled": pos["t2_filled"],
                        "avg_entry": avg or None,
                        "mark": p,
                        "unrealized_pnl_usd": None if unreal is None else round(unreal, 4),
                        "created_at": pos["created_at_iso"],
                    }
                )
        return rows

    def load_trades(self, *, hours: float = 24.0, limit: int | None = 100) -> list[dict[str, Any]]:
        if not TRADES_FILE.exists():
            return []
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        rows: list[dict[str, Any]] = []
        with _lock:
            for line in TRADES_FILE.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                try:
                    ts = datetime.fromisoformat(str(row.get("timestamp")))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                except (TypeError, ValueError):
                    continue
                if ts < cutoff:
                    continue
                rows.append(row)
        rows.sort(key=lambda r: str(r.get("timestamp") or ""), reverse=True)
        return rows[:limit] if limit else rows

    def stats_24h(self) -> dict[str, Any]:
        trades = self.load_trades(hours=24.0, limit=None)
        closed = [t for t in trades if t.get("action") in EXIT_ACTIONS]
        wins = [t for t in closed if float(t.get("pnl_usd") or 0) > 0]
        losses = [t for t in closed if float(t.get("pnl_usd") or 0) < 0]
        # 24h 净盈亏以复式账本为准（含费/滑点/资金费）
        sums = cex_ledger.sum_costs(hours=24.0)
        total_pnl = sums["net_realized"]
        gross_profit = sum(float(t.get("pnl_usd") or 0) for t in wins)
        gross_loss = abs(sum(float(t.get("pnl_usd") or 0) for t in losses))
        avg_win = gross_profit / len(wins) if wins else 0.0
        avg_loss = gross_loss / len(losses) if losses else 0.0
        if avg_loss > 0:
            profit_loss_ratio = avg_win / avg_loss
        elif avg_win > 0:
            profit_loss_ratio = 99.0
        else:
            profit_loss_ratio = 0.0
        open_rows = self.open_positions()
        unreal = self.unrealized_pnl()
        equity = self.equity()
        pnl_pct = (total_pnl / BANKROLL_USD * 100.0) if BANKROLL_USD > 0 else 0.0
        return {
            "window_hours": 24,
            "total_trades": len(closed),
            "open_count": len(open_rows),
            "win_count": len(wins),
            "loss_count": len(losses),
            "win_rate": round(len(wins) / len(closed) * 100.0, 1) if closed else 0.0,
            "total_pnl_usd": round(total_pnl, 2),
            "total_pnl_pct": round(pnl_pct, 2),
            "gross_realized_usd": round(sums["gross_realized"], 2),
            "total_fees_usd": round(sums["fees"], 4),
            "total_slippage_usd": round(sums["slippage"], 4),
            "total_funding_usd": round(sums["funding"], 4),
            "unrealized_pnl_usd": round(unreal, 2),
            "profit_loss_ratio": round(profit_loss_ratio, 2),
            "avg_win_usd": round(avg_win, 2),
            "avg_loss_usd": round(avg_loss, 2),
            "bankroll_usd": BANKROLL_USD,
            "margin_usd": position_margin(),
            "leverage": LEVERAGE,
            "cash_usd": round(self.cash, 2),
            "realized_pnl_usd": round(self.net_realized(), 2),
            "equity_usd": round(equity, 2),
            "audit_ok": bool((self.last_audit or {}).get("ok", True)),
            "current_target": self.current_target,
            "updated_at": _utc_iso(),
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "type": "alt_sim",
            "mode": "sniper_top1",
            "stats_24h": self.stats_24h(),
            "positions": self.open_positions(),
            "trade_log": self.load_trades(hours=24.0, limit=80),
            "last_audit": self.last_audit,
            "config": {
                "bankroll_usd": BANKROLL_USD,
                "margin_usd": position_margin(),
                "position_pct": POSITION_PCT,
                "leverage": LEVERAGE,
                "tranche1_ratio": TRANCHE1_RATIO,
                "tranche2_ratio": TRANCHE2_RATIO,
                "max_open": MAX_OPEN,
                "taker_fee": AL.CEX_TAKER_FEE,
                "slippage_bps_range": [AL.CEX_SLIP_BPS_MIN, AL.CEX_SLIP_BPS_MAX],
                "funding_interval_sec": AL.CEX_FUNDING_INTERVAL_SEC,
                "t2_chase_sec": T2_CHASE_SEC,
            },
            "updated_at": self.updated_at or _utc_iso(),
            "ts": _utc_iso(),
        }

    def clear_trades(self) -> dict[str, Any]:
        with _lock:
            if TRADES_FILE.exists():
                TRADES_FILE.write_text("", encoding="utf-8")
            cex_ledger.clear()
            locked = self.locked_margin()
            self.gross_realized_usd = 0.0
            self.total_fees_usd = 0.0
            self.total_slippage_usd = 0.0
            self.total_funding_usd = 0.0
            self.realized_pnl_usd = 0.0
            self.cash = BANKROLL_USD - locked
            self._persist_account()
        return self.snapshot()

    def trades_to_csv(self, hours: float = 24.0) -> str:
        rows = self.load_trades(hours=hours, limit=None)
        buf = io.StringIO()
        fields = [
            "timestamp", "symbol", "action", "action_label", "leverage",
            "margin_usd", "position_size_usd", "entry_price", "exit_price", "fill_price",
            "gross_pnl_usd", "pnl_usd", "fee_usd", "slippage_usd", "funding_usd",
            "pnl_pct_on_margin", "exit_reason", "sniper_rank", "target_score",
        ]
        writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for r in reversed(rows):
            writer.writerow({k: r.get(k) for k in fields})
        return buf.getvalue()


simulator = AltTop3Simulator()
