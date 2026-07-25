"""费用清算 + 复式记账 + 对账自验证。"""

from __future__ import annotations

import pytest

import alt_sim
import audit_ledger as AL
from alt_sim import AltTop3Simulator
from audit_ledger import DoubleEntryLedger, expected_equity, run_audit_check


@pytest.fixture()
def clean_ledgers(tmp_path, monkeypatch):
    monkeypatch.setattr(AL, "AUDIT_DIR", tmp_path)
    cex = DoubleEntryLedger("cex_sniper", currency="USD")
    pump = DoubleEntryLedger("pump_scavenger", currency="SOL")
    monkeypatch.setattr(AL, "cex_ledger", cex)
    monkeypatch.setattr(AL, "pump_ledger", pump)
    monkeypatch.setattr(alt_sim, "cex_ledger", cex)
    monkeypatch.setattr(alt_sim, "DATA_DIR", tmp_path)
    monkeypatch.setattr(alt_sim, "TRADES_FILE", tmp_path / "trades.jsonl")
    monkeypatch.setattr(alt_sim, "ACCOUNT_FILE", tmp_path / "account.json")
    monkeypatch.setattr(alt_sim, "BANKROLL_USD", 1000.0)
    monkeypatch.setattr(alt_sim, "MARGIN_USD", 100.0)
    monkeypatch.setattr(alt_sim, "POSITION_PCT", None)
    monkeypatch.setattr(alt_sim, "COOLDOWN_MIN", 0.0)
    monkeypatch.setattr(alt_sim, "T2_CHASE_SEC", 9999.0)
    monkeypatch.setattr(AL, "CEX_FUNDING_INTERVAL_SEC", 1.0)  # 测试里快速触发资金费
    return cex, AltTop3Simulator()


def pick(sym="BONK", score=70.0, price=0.0000210, **kw):
    base = {
        "symbol": sym,
        "total_score": score,
        "price": price,
        "tranche_1_price": 0.0000200,
        "tranche_2_price": 0.0000190,
        "stop_loss": 0.0000180,
        "take_profit": 0.0000230,
    }
    base.update(kw)
    return base


def test_cex_costs_nonzero():
    c = AL.cex_trade_costs(notional_usd=1000, side="buy")
    assert c["fee_usd"] > 0
    assert c["slippage_usd"] > 0
    assert 10 <= c["slippage_bps"] <= 30


def test_pump_costs_include_gas():
    c = AL.pump_trade_costs(amount_sol=0.04, side="buy")
    assert c["fee_sol"] > 0
    assert c["gas_sol"] > 0
    assert c["slippage_sol"] > 0


def test_open_deducts_fee_and_slip(clean_ledgers):
    cex, sim = clean_ledgers
    start = sim.equity()
    sim.on_radar_top3([pick()], live_prices={"BONK": 0.000021})
    assert sim.total_fees_usd > 0
    assert sim.total_slippage_usd > 0
    # 开仓后权益应因摩擦略低于本金
    assert sim.equity() < start
    assert abs(sim.equity() - expected_equity(
        initial=1000,
        gross_realized=0,
        fees=sim.total_fees_usd,
        slippage=sim.total_slippage_usd,
        funding=0,
        unrealized=sim.unrealized_pnl(),
    )) < 1e-6


def test_close_audit_identity_holds(clean_ledgers):
    cex, sim = clean_ledgers
    sim.on_radar_top3([pick()], live_prices={"BONK": 0.000021})
    sim.on_prices({"BONK": 0.0000231})  # 止盈
    assert not sim.positions
    result = sim.run_audit(auto_correct=False)
    assert result["ok"] is True
    assert abs(result["delta_equity"]) <= AL.AUDIT_TOLERANCE
    # 净实现 = 毛 - 费 - 滑
    assert sim.net_realized() == pytest.approx(
        sim.gross_realized_usd - sim.total_fees_usd - sim.total_slippage_usd - sim.total_funding_usd,
        abs=1e-8,
    )


def test_audit_detects_leak_and_corrects(clean_ledgers):
    cex, sim = clean_ledgers
    sim.on_radar_top3([pick()], live_prices={"BONK": 0.000021})
    sim.on_prices({"BONK": 0.0000231})
    # 人为制造账目泄漏
    sim.cash += 5.0
    bad = sim.run_audit(auto_correct=False)
    assert bad["ok"] is False
    assert "AUDIT ERROR" in (bad.get("alert") or "")
    # 自动修正
    fixed = sim.run_audit(auto_correct=True)
    assert fixed.get("corrected") is True
    assert abs(sim.equity() - expected_equity(
        initial=1000,
        gross_realized=sim.gross_realized_usd,
        fees=sim.total_fees_usd,
        slippage=sim.total_slippage_usd,
        funding=sim.total_funding_usd,
        unrealized=0,
    )) <= AL.AUDIT_TOLERANCE


def test_funding_recorded(clean_ledgers):
    cex, sim = clean_ledgers
    sim.on_radar_top3([pick()], live_prices={"BONK": 0.000021})
    pos = sim.positions["BONK"]
    import time as _t
    ev = sim._apply_funding(pos, _t.time() + AL.CEX_FUNDING_INTERVAL_SEC + 10)
    # 若间隔仍被挡住，强制拨回 last_funding_at
    if ev is None:
        pos["last_funding_at"] = 0.0
        ev = sim._apply_funding(pos, _t.time())
    assert ev is not None and ev.get("action") == "funding"
    assert sim.total_funding_usd > 0
    sums = cex.sum_costs()
    assert sums["funding"] > 0


def test_24h_report_fields(clean_ledgers):
    _, sim = clean_ledgers
    sim.on_radar_top3([pick()], live_prices={"BONK": 0.000021})
    sim.on_prices({"BONK": 0.0000231})
    report = sim.audit_report_24h()
    for key in (
        "initial_capital", "net_profit", "total_fees", "total_slippage",
        "total_funding", "equity", "audit_ok",
    ):
        assert key in report
    assert report["initial_capital"] == 1000.0
    assert report["total_fees"] > 0
    csv = AL.report_to_csv(report)
    assert "net_profit" in csv


def test_run_audit_check_helper(tmp_path, monkeypatch):
    monkeypatch.setattr(AL, "AUDIT_DIR", tmp_path)
    led = DoubleEntryLedger("unit_test", currency="USD")
    led.append({"kind": "gross_pnl", "amount": 10.0})
    led.append({"kind": "fee", "amount": 1.0})
    led.append({"kind": "slippage", "amount": 0.5})
    ok = run_audit_check(
        led, initial=100, displayed_equity=108.5, displayed_realized_net=8.5, unrealized=0,
    )
    assert ok["ok"] is True
    fail = run_audit_check(
        led, initial=100, displayed_equity=120, displayed_realized_net=8.5, unrealized=0,
        auto_correct=True,
    )
    assert fail["ok"] is False
    assert fail["correction"]["equity"] == pytest.approx(108.5)
