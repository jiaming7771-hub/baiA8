"""#1 极速狙击模拟仓：市价快成交 / 单标的 / 轮换 / 权益清算 / 盈亏比。"""

from __future__ import annotations

import pytest

import alt_sim
import audit_ledger as AL
from alt_sim import AltTop3Simulator
from audit_ledger import DoubleEntryLedger


@pytest.fixture()
def sim(tmp_path, monkeypatch):
    monkeypatch.setattr(AL, "AUDIT_DIR", tmp_path)
    cex = DoubleEntryLedger("cex_sniper", currency="USD")
    monkeypatch.setattr(AL, "cex_ledger", cex)
    monkeypatch.setattr(alt_sim, "cex_ledger", cex)
    monkeypatch.setattr(alt_sim, "DATA_DIR", tmp_path)
    monkeypatch.setattr(alt_sim, "TRADES_FILE", tmp_path / "trades.jsonl")
    monkeypatch.setattr(alt_sim, "ACCOUNT_FILE", tmp_path / "account.json")
    monkeypatch.setattr(alt_sim, "BANKROLL_USD", 1000.0)
    monkeypatch.setattr(alt_sim, "MARGIN_USD", 100.0)
    monkeypatch.setattr(alt_sim, "POSITION_PCT", None)
    monkeypatch.setattr(alt_sim, "COOLDOWN_MIN", 0.0)
    monkeypatch.setattr(alt_sim, "T2_CHASE_SEC", 9999.0)
    monkeypatch.setattr(AL, "CEX_FUNDING_INTERVAL_SEC", 99999.0)
    return AltTop3Simulator()


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


def test_only_opens_rank1_not_all_top3(sim):
    events = sim.on_radar_top3(
        [pick("BONK", 70), pick("NEIRO", 65), pick("BOME", 64)],
        live_prices={"BONK": 0.0000210, "NEIRO": 0.00005, "BOME": 0.0004},
    )
    assert "BONK" in sim.positions
    assert "NEIRO" not in sim.positions
    assert "BOME" not in sim.positions
    assert any(e["action"] == "open_t1" for e in events)
    assert sim.positions["BONK"]["t1_filled"] is True
    assert sim.positions["BONK"]["status"] == "open"


def test_fast_fill_at_mark_not_waiting_limit(sim):
    sim.on_radar_top3([pick(price=0.0000220)], live_prices={"BONK": 0.0000220})
    pos = sim.positions["BONK"]
    assert pos["t1_filled"]
    assert pos["avg_entry"] == pytest.approx(0.0000220, rel=1e-9)
    assert pos["filled_notional"] == pytest.approx(100 * 10 * 0.30)


def test_only_one_position_max(sim):
    sim.on_radar_top3([pick("BONK", 70)], live_prices={"BONK": 0.000021})
    sim.on_radar_top3(
        [pick("NEIRO", 80, price=0.00005, tranche_1_price=0.000049, tranche_2_price=0.000048,
              stop_loss=0.000046, take_profit=0.000055)],
        live_prices={"NEIRO": 0.00005, "BONK": 0.000021},
    )
    assert len(sim.positions) <= 1
    assert "NEIRO" in sim.positions
    assert "BONK" not in sim.positions


def test_keep_winner_when_new_score_not_enough(sim):
    sim.on_radar_top3([pick("BONK", 70, price=0.000021)], live_prices={"BONK": 0.000021})
    sim.positions["BONK"]["mark"] = 0.0000225
    sim.on_radar_top3(
        [pick("NEIRO", 70.2, price=0.00005, tranche_1_price=0.000049, tranche_2_price=0.000048,
              stop_loss=0.000046, take_profit=0.000055)],
        live_prices={"NEIRO": 0.00005, "BONK": 0.0000225},
    )
    assert "BONK" in sim.positions
    assert "NEIRO" not in sim.positions


def test_take_profit_settles_equity(sim):
    sim.on_radar_top3([pick()], live_prices={"BONK": 0.000021})
    events = sim.on_prices({"BONK": 0.0000231})
    tp = [e for e in events if e["action"] == "take_profit"]
    assert tp and tp[0]["pnl_usd"] is not None
    assert not sim.positions
    assert sim.equity() == pytest.approx(1000.0 + sim.net_realized(), abs=1e-4)
    assert sim.run_audit(auto_correct=False)["ok"] is True


def test_hard_stop_reduces_equity(sim):
    sim.on_radar_top3([pick()], live_prices={"BONK": 0.000021})
    sim.on_prices({"BONK": 0.0000189})
    before = sim.equity()
    events = sim.on_prices({"BONK": 0.0000175})
    assert any(e["action"] in ("hard_stop", "liquidation") for e in events)
    assert sim.equity() < before or sim.net_realized() < 0


def test_stats_include_rr_and_pnl_pct(sim):
    sim.on_radar_top3([pick()], live_prices={"BONK": 0.000021})
    sim.on_prices({"BONK": 0.0000231})
    stats = sim.stats_24h()
    assert stats["total_trades"] == 1
    assert "profit_loss_ratio" in stats
    assert stats["total_fees_usd"] > 0
    assert stats["equity_usd"] == pytest.approx(sim.equity(), abs=0.05)
    assert stats["bankroll_usd"] == 1000.0


def test_position_pct_sizing(tmp_path, monkeypatch):
    monkeypatch.setattr(AL, "AUDIT_DIR", tmp_path)
    cex = DoubleEntryLedger("cex_sniper", currency="USD")
    monkeypatch.setattr(AL, "cex_ledger", cex)
    monkeypatch.setattr(alt_sim, "cex_ledger", cex)
    monkeypatch.setattr(alt_sim, "DATA_DIR", tmp_path)
    monkeypatch.setattr(alt_sim, "TRADES_FILE", tmp_path / "trades.jsonl")
    monkeypatch.setattr(alt_sim, "ACCOUNT_FILE", tmp_path / "account.json")
    monkeypatch.setattr(alt_sim, "BANKROLL_USD", 2000.0)
    monkeypatch.setattr(alt_sim, "POSITION_PCT", 0.05)
    monkeypatch.setattr(alt_sim, "COOLDOWN_MIN", 0.0)
    monkeypatch.setattr(AL, "CEX_FUNDING_INTERVAL_SEC", 99999.0)
    s = AltTop3Simulator()
    s.on_radar_top3([pick()], live_prices={"BONK": 0.000021})
    assert s.positions["BONK"]["margin_usd"] == pytest.approx(100.0)


def test_csv_has_sniper_fields(sim):
    sim.on_radar_top3([pick()], live_prices={"BONK": 0.000021})
    sim.on_prices({"BONK": 0.0000231})
    header = sim.trades_to_csv().splitlines()[0]
    for field in ("sniper_rank", "pnl_usd", "fee_usd", "slippage_usd", "exit_reason"):
        assert field in header
