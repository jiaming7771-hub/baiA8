"""实盘硬风控单测：滑点 / 仓位 / 回撤与绝对亏损熔断 / 四层出场。"""

from __future__ import annotations

import time

import pytest

from pumpfun import config as C
from pumpfun.execution import PaperBroker
from pumpfun.risk import RiskBlocked, RiskGuard


def test_slippage_clamped_to_5_10_percent():
    g = RiskGuard()
    assert g.clamp_slippage_bps(100) == 500   # 低于 5% → 抬到 5%
    assert g.clamp_slippage_bps(500) == 500
    assert g.clamp_slippage_bps(800) == 800
    assert g.clamp_slippage_bps(5000) == 1000  # 超过 10% → 砍到 10%


def test_position_size_hard_caps():
    g = RiskGuard()
    # equity=2 SOL → 1%=0.02，夹在 0.02~0.04
    s = g.clamp_position_sol(1.0, equity=2.0, cash=2.0)
    assert 0.02 <= s <= 0.04
    # equity=10 → 1%=0.1 但硬顶 0.04
    s2 = g.clamp_position_sol(1.0, equity=10.0, cash=10.0)
    assert s2 == pytest.approx(0.04)
    # 现金不足
    with pytest.raises(RiskBlocked):
        g.clamp_position_sol(1.0, equity=10.0, cash=0.01)


def test_drawdown_halts_new_opens():
    g = RiskGuard()
    g.update_equity(10.0)
    g.update_equity(9.5)  # -5% 未触发
    assert g.drawdown_halted is False
    g.update_equity(8.0)  # -20% ≥ 15%
    assert g.drawdown_halted is True
    with pytest.raises(RiskBlocked):
        g.assert_can_open(equity=8.0)
    # 卖出侧仍可更新，不因熔断挡住 gate(sell)
    out = g.pre_trade_gate(side="sell", equity=8.0, cash=8.0, amount_sol=0.03)
    assert out["side"] == "sell"


def test_abs_loss_halts_even_if_pct_small(monkeypatch):
    """峰值很高时，绝对亏 0.6 SOL 也应熔断（即使回撤%未到 15%）。"""
    monkeypatch.setattr(C, "DRAWDOWN_HALT", 0.50)  # 故意抬高%阈值
    monkeypatch.setattr(C, "ABS_LOSS_HALT_SOL", 0.6)
    g = RiskGuard()
    g.update_equity(10.0)
    g.update_equity(9.5)  # 亏 0.5 < 0.6
    assert g.drawdown_halted is False
    g.update_equity(9.3)  # 亏 0.7 ≥ 0.6
    assert g.drawdown_halted is True
    assert "绝对亏损" in (g.halt_reason or "")


def test_pre_trade_buy_blocked_when_halted():
    g = RiskGuard()
    g.update_equity(10.0)
    g.update_equity(8.0)
    with pytest.raises(RiskBlocked):
        g.pre_trade_gate(side="buy", equity=8.0, cash=8.0, amount_sol=0.03)


def test_config_exit_defaults_match_spec():
    # 黄金猎杀规格：-25% 硬止损 / +28% TP1 / 回撤13% / 11分钟
    assert C.HARD_STOP_PCT == pytest.approx(0.25)
    assert C.TP1_PCT == pytest.approx(0.28)
    assert C.TP1_SELL_RATIO == pytest.approx(0.55)
    assert C.TRAIL_DRAWDOWN == pytest.approx(0.13)
    assert C.TIME_STOP_MINUTES == pytest.approx(11.0)
    assert C.DRAWDOWN_HALT == pytest.approx(0.15)
    assert C.ABS_LOSS_HALT_SOL == pytest.approx(0.6)
    assert C.MAX_OPEN_POSITIONS == 3
    # 超跌区间 [40%, 80%]，m15 恐慌/鲸抛 + m5 活跃度硬门槛
    assert C.ATH_DROP_MIN == pytest.approx(0.40)
    assert C.ATH_DROP_MAX == pytest.approx(0.80)
    assert C.PANIC_RATIO_MIN == pytest.approx(1.2)
    assert C.WHALE_DUMP_MIN == pytest.approx(0.40)
    assert C.LIQUIDITY_MIN_SOL == pytest.approx(5.0)
    assert C.MIN_TX_M5 == 5
    assert C.MIN_VOLUME_M5_SOL == pytest.approx(1.5)


def test_reject_dead_coin_over_max_drop():
    """跌幅 > 80% 的归零死币必须被拒。"""
    from pumpfun.strategy import Candidate, pass_hard_filters
    import time as _t

    dead = Candidate(
        mint="dead", symbol="DEAD", listed_at=_t.time() - 30 * 60,
        ath_price=1.0, price=0.05,  # -95%
        buy_vol=10, sell_vol=20, whale_dump_pct=0.6,
        liquidity_sol=20.0, tx_count_m5=10, volume_m5_sol=2.0,
    )
    ok, fails = pass_hard_filters(dead)
    assert not ok
    assert any("死币" in f for f in fails)


def test_reject_illiquid_and_inactive():
    """流动性或近5m活跃度枯竭 = 拉闸死币，必须拒。"""
    from pumpfun.strategy import Candidate, pass_hard_filters
    import time as _t

    illiquid = Candidate(
        mint="ill", symbol="ILL", listed_at=_t.time() - 30 * 60,
        ath_price=1.0, price=0.4,  # -60% 甜点
        buy_vol=10, sell_vol=20, whale_dump_pct=0.6,
        liquidity_sol=0.5, tx_count_m5=0, volume_m5_sol=0.0,
    )
    ok, fails = pass_hard_filters(illiquid)
    assert not ok
    assert any("流动性" in f for f in fails)
    assert any("成交" in f for f in fails)


def test_accept_golden_dip():
    """-60% 超跌 + 活跃盘口 = 黄金猎杀目标，应通过。"""
    from pumpfun.strategy import Candidate, pass_hard_filters
    import time as _t

    good = Candidate(
        mint="good", symbol="GOOD", listed_at=_t.time() - 30 * 60,
        ath_price=1.0, price=0.4,  # -60%
        buy_vol=15, sell_vol=25, whale_dump_pct=0.5,  # 恐慌比 1.67
        liquidity_sol=20.0, tx_count_m5=8, volume_m5_sol=2.0,
    )
    ok, fails = pass_hard_filters(good)
    assert ok, fails


def test_hard_stop_fires_before_time_stop(tmp_path, monkeypatch):
    """浮亏 -25% 必须立刻斩仓，哪怕开仓不足 11 分钟。"""
    monkeypatch.setattr(C, "ACCOUNT_FILE", tmp_path / "account.json")
    monkeypatch.setattr(C, "DATA_DIR", tmp_path)
    monkeypatch.setattr(C, "DAILY_TRADES_FILE", tmp_path / "daily.jsonl")
    monkeypatch.setattr(C, "TRADES_FILE", tmp_path / "trades.jsonl")
    monkeypatch.setattr(C, "HARD_STOP_PCT", 0.25)
    monkeypatch.setattr(C, "TIME_STOP_MINUTES", 11.0)

    broker = PaperBroker()
    broker.dry_run = True
    broker.cash = 10.0
    mint = "TestMintHardStop"
    entry = 1.0
    broker.positions[mint] = {
        "id": "hs1",
        "mint": mint,
        "symbol": "HST",
        "entry": entry,
        "qty": 0.03,
        "qty_left": 0.03,
        "sol_spent": 0.03,
        "opened_at": time.time() - 60,  # 仅 1 分钟
        "peak": entry,
        "tp1_done": False,
        "trail_line": None,
        "dry_run": True,
        "fees_sol": 0.0,
        "gas_sol": 0.0,
        "slippage_sol": 0.0,
        "slippage_bps": 500,
    }
    events = broker.manage({mint: 0.74})  # -26%
    assert any(e["type"] == "hard_stop" for e in events)
    assert mint not in broker.positions


def test_tp1_at_plus_28(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "ACCOUNT_FILE", tmp_path / "account.json")
    monkeypatch.setattr(C, "DATA_DIR", tmp_path)
    monkeypatch.setattr(C, "DAILY_TRADES_FILE", tmp_path / "daily.jsonl")
    monkeypatch.setattr(C, "TRADES_FILE", tmp_path / "trades.jsonl")
    monkeypatch.setattr(C, "TP1_PCT", 0.28)
    monkeypatch.setattr(C, "TP1_SELL_RATIO", 0.55)
    monkeypatch.setattr(C, "HARD_STOP_PCT", 0.25)

    broker = PaperBroker()
    broker.dry_run = True
    broker.cash = 10.0
    mint = "TestMintTP1"
    entry = 1.0
    broker.positions[mint] = {
        "id": "tp1",
        "mint": mint,
        "symbol": "TP1",
        "entry": entry,
        "qty": 1.0,
        "qty_left": 1.0,
        "sol_spent": 1.0,
        "opened_at": time.time(),
        "peak": entry,
        "tp1_done": False,
        "trail_line": None,
        "dry_run": True,
        "fees_sol": 0.0,
        "gas_sol": 0.0,
        "slippage_sol": 0.0,
        "slippage_bps": 500,
    }
    events = broker.manage({mint: 1.28})  # +28%
    assert any(e["type"] == "tp1" for e in events)
    assert broker.positions[mint]["tp1_done"] is True
    assert broker.positions[mint]["qty_left"] == pytest.approx(0.45)


def test_time_stop_preempts_tp1(tmp_path, monkeypatch):
    """满 11 分钟即使涨到 TP1，也应直接时间清仓，不先部分止盈。"""
    monkeypatch.setattr(C, "ACCOUNT_FILE", tmp_path / "account.json")
    monkeypatch.setattr(C, "DATA_DIR", tmp_path)
    monkeypatch.setattr(C, "DAILY_TRADES_FILE", tmp_path / "daily.jsonl")
    monkeypatch.setattr(C, "TRADES_FILE", tmp_path / "trades.jsonl")

    broker = PaperBroker()
    broker.dry_run = True
    mint = "TimeBeforeTp1"
    broker.positions[mint] = {
        "id": "time1",
        "mint": mint,
        "symbol": "TIME",
        "entry": 1.0,
        "qty": 1.0,
        "qty_left": 1.0,
        "sol_spent": 1.0,
        "opened_at": time.time() - (C.TIME_STOP_MINUTES + 0.1) * 60,
        "peak": 1.0,
        "tp1_done": False,
        "trail_line": None,
        "dry_run": True,
        "fees_sol": 0.0,
        "gas_sol": 0.0,
        "slippage_sol": 0.0,
        "slippage_bps": 500,
    }
    events = broker.manage({mint: 1.40})
    assert [event["type"] for event in events] == ["time_stop"]
    assert mint not in broker.positions
