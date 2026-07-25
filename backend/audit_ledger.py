"""费用清算 + 复式记账 + 自动化对账自验证。

恒等式：
  Equity = Initial + GrossRealizedPnL - Fees - Slippage - Funding + UnrealizedPnL

任何一边加总与账户显示不一致（|Δ| > 容差）即触发 [AUDIT ERROR] 并尝试修正。
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("audit_ledger")

ROOT = Path(__file__).resolve().parent
AUDIT_DIR = ROOT / "audit_data"
AUDIT_DIR.mkdir(parents=True, exist_ok=True)

# ---------- CEX 合约费用默认 ----------
CEX_TAKER_FEE = float(os.getenv("CEX_TAKER_FEE", "0.0004"))  # 0.04%
CEX_MAKER_FEE = float(os.getenv("CEX_MAKER_FEE", "0.0002"))  # 0.02%
CEX_SLIP_BPS_MIN = float(os.getenv("CEX_SLIP_BPS_MIN", "10"))  # 0.10%
CEX_SLIP_BPS_MAX = float(os.getenv("CEX_SLIP_BPS_MAX", "30"))  # 0.30%
CEX_FUNDING_INTERVAL_SEC = float(os.getenv("CEX_FUNDING_INTERVAL_SEC", str(8 * 3600)))
CEX_DEFAULT_FUNDING_RATE = float(os.getenv("CEX_DEFAULT_FUNDING_RATE", "0.0001"))  # 每期 0.01%

# ---------- Pump / DEX 费用默认 ----------
PUMP_DEX_FEE = float(os.getenv("PUMP_DEX_FEE", "0.0025"))  # 0.25%
PUMP_GAS_SOL = float(os.getenv("PUMP_GAS_SOL", "0.0002"))  # 单笔优先费+基础费
PUMP_SLIP_MIN = float(os.getenv("PUMP_SLIP_MIN", "0.001"))  # 0.1%
PUMP_SLIP_MAX = float(os.getenv("PUMP_SLIP_MAX", "0.003"))  # 0.3%

# 对账容差：0.0001 U 或 0.0001 SOL
AUDIT_TOLERANCE = float(os.getenv("AUDIT_TOLERANCE", "0.0001"))
AUDIT_INTERVAL_SEC = float(os.getenv("AUDIT_INTERVAL_SEC", str(3600)))

_lock = threading.Lock()


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


# =====================================================================
# 费用模型
# =====================================================================

def cex_slippage_bps(notional_usd: float) -> float:
    """山寨合约滑点：名义越大略增，夹在 10~30 bps。"""
    n = max(0.0, float(notional_usd))
    # 100U 名义 → 约 10bps；10000U → 逼近 30bps
    bps = CEX_SLIP_BPS_MIN + min(CEX_SLIP_BPS_MAX - CEX_SLIP_BPS_MIN, n / 500.0)
    return max(CEX_SLIP_BPS_MIN, min(CEX_SLIP_BPS_MAX, bps))


def cex_trade_costs(
    *,
    notional_usd: float,
    side: str = "buy",
    is_taker: bool = True,
) -> dict[str, float]:
    """CEX 开/平仓：手续费 + 滑点损耗（均以 USD 计）。"""
    notional = abs(float(notional_usd))
    fee_rate = CEX_TAKER_FEE if is_taker else CEX_MAKER_FEE
    fee = notional * fee_rate
    slip_bps = cex_slippage_bps(notional)
    slip = notional * (slip_bps / 10000.0)
    return {
        "notional_usd": round(notional, 8),
        "fee_usd": round(fee, 8),
        "fee_rate": fee_rate,
        "slippage_usd": round(slip, 8),
        "slippage_bps": round(slip_bps, 4),
        "total_friction_usd": round(fee + slip, 8),
        "side": side,
        "is_taker": is_taker,
    }


def cex_fill_price(mid: float, *, side: str, slip_bps: float) -> float:
    mid = float(mid)
    adj = slip_bps / 10000.0
    if side == "buy":
        return mid * (1.0 + adj)
    return mid * (1.0 - adj)


def cex_funding_fee(*, notional_usd: float, funding_rate: float | None = None) -> float:
    """多头：正费率付费，负费率收费。返回应扣金额（正数=从账户扣）。"""
    rate = CEX_DEFAULT_FUNDING_RATE if funding_rate is None else float(funding_rate)
    return round(abs(float(notional_usd)) * rate, 8)


def pump_slippage_pct(amount_sol: float, liquidity_sol: float | None = None) -> float:
    """土狗池滑点：按成交占池比例放大，夹在 0.1%~0.3%。"""
    amt = abs(float(amount_sol))
    liq = float(liquidity_sol) if liquidity_sol and liquidity_sol > 0 else 50.0
    impact = amt / liq
    pct = PUMP_SLIP_MIN + min(PUMP_SLIP_MAX - PUMP_SLIP_MIN, impact * 0.5)
    return max(PUMP_SLIP_MIN, min(PUMP_SLIP_MAX, pct))


def pump_trade_costs(
    *,
    amount_sol: float,
    side: str = "buy",
    liquidity_sol: float | None = None,
) -> dict[str, float]:
    """Pump/DEX：DEX 手续费 + Gas + 滑点。"""
    amt = abs(float(amount_sol))
    dex_fee = amt * PUMP_DEX_FEE
    gas = PUMP_GAS_SOL
    slip_pct = pump_slippage_pct(amt, liquidity_sol)
    slip = amt * slip_pct
    return {
        "amount_sol": round(amt, 8),
        "fee_sol": round(dex_fee, 8),
        "gas_sol": round(gas, 8),
        "slippage_sol": round(slip, 8),
        "slippage_pct": round(slip_pct, 6),
        "total_friction_sol": round(dex_fee + gas + slip, 8),
        "side": side,
    }


def pump_fill_price(mid: float, *, side: str, slip_pct: float) -> float:
    mid = float(mid)
    if side == "buy":
        return mid * (1.0 + slip_pct)
    return mid * (1.0 - slip_pct)


# =====================================================================
# 复式记账账本
# =====================================================================

class DoubleEntryLedger:
    """按模块隔离的 JSONL 复式流水。"""

    def __init__(self, module: str, *, currency: str = "USD") -> None:
        self.module = module
        self.currency = currency
        self.path = AUDIT_DIR / f"{module}_ledger.jsonl"
        self.alert_path = AUDIT_DIR / f"{module}_audit_alerts.log"

    def append(self, entry: dict[str, Any]) -> dict[str, Any]:
        row = {
            "timestamp": _utc_iso(),
            "module": self.module,
            "currency": self.currency,
            **entry,
        }
        with _lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row

    def load(self, *, hours: float | None = None) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        cutoff = None
        if hours is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        rows: list[dict[str, Any]] = []
        with _lock:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if cutoff is not None:
                    ts = _parse_ts(row.get("timestamp"))
                    if ts is None or ts < cutoff:
                        continue
                rows.append(row)
        return rows

    def clear(self) -> None:
        with _lock:
            if self.path.exists():
                self.path.write_text("", encoding="utf-8")

    def sum_costs(self, *, hours: float | None = None) -> dict[str, float]:
        rows = self.load(hours=hours)
        gross = 0.0
        fees = 0.0
        slip = 0.0
        funding = 0.0
        gas = 0.0
        for r in rows:
            kind = r.get("kind") or r.get("entry_type")
            if kind in ("realized_pnl", "gross_pnl"):
                gross += float(r.get("amount") or 0)
            elif kind in ("fee", "trading_fee", "dex_fee"):
                fees += abs(float(r.get("amount") or 0))
            elif kind in ("slippage",):
                slip += abs(float(r.get("amount") or 0))
            elif kind in ("funding",):
                funding += float(r.get("amount") or 0)  # 可正可负：付为正
            elif kind in ("gas",):
                gas += abs(float(r.get("amount") or 0))
        return {
            "gross_realized": round(gross, 8),
            "fees": round(fees, 8),
            "slippage": round(slip, 8),
            "funding": round(funding, 8),
            "gas": round(gas, 8),
            "net_realized": round(gross - fees - slip - funding - gas, 8),
            "entry_count": len(rows),
        }

    def write_alert(self, msg: str) -> None:
        line = f"{_utc_iso()} {msg}\n"
        with _lock:
            with self.alert_path.open("a", encoding="utf-8") as f:
                f.write(line)
        logger.error(msg)


def expected_equity(
    *,
    initial: float,
    gross_realized: float,
    fees: float,
    slippage: float,
    funding: float,
    unrealized: float,
    gas: float = 0.0,
) -> float:
    """复式恒等式右侧。"""
    return float(initial) + float(gross_realized) - float(fees) - float(slippage) - float(funding) - float(gas) + float(unrealized)


def run_audit_check(
    ledger: DoubleEntryLedger,
    *,
    initial: float,
    displayed_equity: float,
    displayed_realized_net: float | None = None,
    unrealized: float = 0.0,
    hours: float | None = None,
    auto_correct: bool = True,
) -> dict[str, Any]:
    """遍历账本加总，与账户显示对账；失败则告警并可返回修正建议。"""
    sums = ledger.sum_costs(hours=hours)
    expected = expected_equity(
        initial=initial,
        gross_realized=sums["gross_realized"],
        fees=sums["fees"],
        slippage=sums["slippage"],
        funding=sums["funding"],
        unrealized=unrealized,
        gas=sums["gas"],
    )
    delta_eq = float(displayed_equity) - expected
    net_from_ledger = sums["net_realized"]
    delta_realized = None
    if displayed_realized_net is not None:
        delta_realized = float(displayed_realized_net) - net_from_ledger

    ok = abs(delta_eq) <= AUDIT_TOLERANCE and (
        delta_realized is None or abs(delta_realized) <= AUDIT_TOLERANCE
    )

    result: dict[str, Any] = {
        "ok": ok,
        "module": ledger.module,
        "currency": ledger.currency,
        "initial": round(initial, 8),
        "ledger_sums": sums,
        "unrealized": round(unrealized, 8),
        "expected_equity": round(expected, 8),
        "displayed_equity": round(float(displayed_equity), 8),
        "delta_equity": round(delta_eq, 8),
        "displayed_realized_net": None if displayed_realized_net is None else round(float(displayed_realized_net), 8),
        "delta_realized": None if delta_realized is None else round(delta_realized, 8),
        "tolerance": AUDIT_TOLERANCE,
        "ts": _utc_iso(),
    }

    if not ok:
        msg = (
            f"[AUDIT ERROR] 账目对账失败，存在资金泄漏或漏记！ "
            f"module={ledger.module} Δequity={delta_eq:.6f} "
            f"displayed={displayed_equity:.6f} expected={expected:.6f} "
            f"sums={sums}"
        )
        ledger.write_alert(msg)
        result["alert"] = msg
        if auto_correct:
            # 修正建议：以账本为准重建净已实现与权益
            result["correction"] = {
                "realized_net": net_from_ledger,
                "equity": expected,
                "cash_hint": expected - unrealized,  # 无在仓市值拆分时的现金近似
            }
            logger.warning(
                "[AUDIT] auto-correct suggested realized_net=%.6f equity=%.6f",
                net_from_ledger, expected,
            )
    else:
        logger.info(
            "[AUDIT OK] %s equity=%.6f net_realized=%.6f fees=%.6f slip=%.6f funding=%.6f",
            ledger.module, expected, net_from_ledger, sums["fees"], sums["slippage"], sums["funding"],
        )
    return result


def build_24h_audit_report(
    ledger: DoubleEntryLedger,
    *,
    initial: float,
    displayed_equity: float,
    unrealized: float = 0.0,
    win_rate: float | None = None,
    total_trades: int | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """24h 详细审计财务报表。"""
    sums = ledger.sum_costs(hours=24.0)
    audit = run_audit_check(
        ledger,
        initial=initial,
        displayed_equity=displayed_equity,
        displayed_realized_net=sums["net_realized"],  # 自洽校验
        unrealized=unrealized,
        hours=24.0,
        auto_correct=False,
    )
    net_profit = sums["net_realized"]
    report = {
        "report_type": "24h_audit_financial",
        "module": ledger.module,
        "currency": ledger.currency,
        "window_hours": 24,
        "initial_capital": round(initial, 6),
        "net_profit": round(net_profit, 6),
        "gross_realized_pnl": sums["gross_realized"],
        "total_fees": sums["fees"],
        "total_slippage": sums["slippage"],
        "total_funding": sums["funding"],
        "total_gas": sums["gas"],
        "total_friction": round(sums["fees"] + sums["slippage"] + abs(sums["funding"]) + sums["gas"], 6),
        "unrealized_pnl": round(unrealized, 6),
        "equity": round(float(displayed_equity), 6),
        "expected_equity": audit["expected_equity"],
        "audit_ok": audit["ok"],
        "delta_equity": audit["delta_equity"],
        "win_rate": win_rate,
        "total_trades": total_trades,
        "generated_at": _utc_iso(),
        **(extra or {}),
    }
    return report


def report_to_csv(report: dict[str, Any], ledger_rows: list[dict[str, Any]] | None = None) -> str:
    buf = io.StringIO()
    buf.write("# 24h Audit Financial Report\n")
    for k in (
        "module", "currency", "initial_capital", "net_profit", "gross_realized_pnl",
        "total_fees", "total_slippage", "total_funding", "total_gas", "total_friction",
        "unrealized_pnl", "equity", "expected_equity", "audit_ok", "delta_equity",
        "win_rate", "total_trades", "generated_at",
    ):
        if k in report:
            buf.write(f"{k},{report[k]}\n")
    if ledger_rows:
        buf.write("\n# Ledger Entries (24h)\n")
        fields = ["timestamp", "kind", "amount", "symbol", "position_id", "note", "meta"]
        writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for r in ledger_rows:
            writer.writerow({
                "timestamp": r.get("timestamp"),
                "kind": r.get("kind"),
                "amount": r.get("amount"),
                "symbol": r.get("symbol"),
                "position_id": r.get("position_id"),
                "note": r.get("note"),
                "meta": json.dumps(r.get("meta") or {}, ensure_ascii=False),
            })
    return buf.getvalue()


# 预置两个模块账本
cex_ledger = DoubleEntryLedger("cex_sniper", currency="USD")
pump_ledger = DoubleEntryLedger("pump_scavenger", currency="SOL")
