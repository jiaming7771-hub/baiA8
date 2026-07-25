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
    # 动量默认：-13% 硬止损 / +22% TP1卖50% / 回撤9% / 12分钟
    assert C.STRATEGY_MODE == "momentum"
    assert C.IS_MOMENTUM is True
    assert C.HARD_STOP_PCT == pytest.approx(0.13)
    assert C.TP1_PCT == pytest.approx(0.22)
    assert C.TP1_SELL_RATIO == pytest.approx(0.50)
    assert C.TRAIL_DRAWDOWN == pytest.approx(0.09)
    assert C.TIME_STOP_MINUTES == pytest.approx(12.0)
    assert C.DRAWDOWN_HALT == pytest.approx(0.15)
    assert C.ABS_LOSS_HALT_SOL == pytest.approx(0.6)
    assert C.MAX_OPEN_POSITIONS == 3
    assert C.AGE_MIN_MINUTES == pytest.approx(8.0)
    assert C.AGE_MAX_MINUTES == pytest.approx(120.0)
    assert C.REBOUND_MIN == pytest.approx(0.20)
    assert C.REBOUND_MAX == pytest.approx(0.40)
    assert C.BUY_SELL_RATIO_MIN == pytest.approx(1.3)
    assert C.PULLBACK_MAX == pytest.approx(0.15)
    assert C.LIQUIDITY_MIN_SOL == pytest.approx(10.0)
    assert C.MIN_TX_M5 == 15
    assert C.MIN_VOLUME_M5_SOL == pytest.approx(5.0)
    assert C.AGE_EXEMPT_VOLUME_M5_SOL == pytest.approx(100.0)
    assert C.AGE_EXEMPT_TX_M5 == 200
    assert C.AGE_EXEMPT_BUY_SELL_RATIO == pytest.approx(3.0)


def test_reject_deep_pullback_not_momentum():
    """距高点回撤 > 15% 不算主升攻击区，必须拒。"""
    from pumpfun.strategy import Candidate, pass_hard_filters
    import time as _t

    deep = Candidate(
        mint="deep", symbol="DEEP", listed_at=_t.time() - 30 * 60,
        ath_price=1.0, price=0.80,  # 回撤 20%
        buy_vol=30, sell_vol=15, whale_dump_pct=0.0,
        liquidity_sol=25.0, tx_count_m5=45, volume_m5_sol=8.0,
        buys_m5=30, sells_m5=15, chg_m5=5.0, chg_m15=32.0, chg_m30=30.0,
        price_streak=2,
    )
    ok, fails = pass_hard_filters(deep)
    assert not ok
    assert any("回撤" in f for f in fails)


def test_reject_illiquid_and_inactive():
    """流动性或近5m活跃度枯竭 = 死水盘，必须拒。"""
    from pumpfun.strategy import Candidate, pass_hard_filters
    import time as _t

    illiquid = Candidate(
        mint="ill", symbol="ILL", listed_at=_t.time() - 30 * 60,
        ath_price=1.0, price=0.92,
        buy_vol=30, sell_vol=15, whale_dump_pct=0.0,
        liquidity_sol=0.5, tx_count_m5=0, volume_m5_sol=0.0,
        buys_m5=0, sells_m5=0, chg_m5=5.0, chg_m15=32.0, chg_m30=30.0,
        price_streak=2,
    )
    ok, fails = pass_hard_filters(illiquid)
    assert not ok
    assert any("流动性" in f for f in fails)


def test_accept_momentum_setup():
    """回升甜点 + 买盘主导 + 活盘 + 贴近高点 = 应通过。"""
    from pumpfun.strategy import Candidate, pass_hard_filters
    import time as _t

    good = Candidate(
        mint="good", symbol="GOOD", listed_at=_t.time() - 30 * 60,
        ath_price=1.0, price=0.92,  # 回撤 8%
        buy_vol=30, sell_vol=15, whale_dump_pct=0.0,
        liquidity_sol=25.0, tx_count_m5=45, volume_m5_sol=8.0,
        buys_m5=30, sells_m5=15, chg_m5=5.0, chg_m15=32.0, chg_m30=30.0,
        price_streak=2,
    )
    ok, fails = pass_hard_filters(good)
    assert ok, fails


def test_hard_stop_fires_before_time_stop(tmp_path, monkeypatch):
    """浮亏达到硬止损必须立刻斩仓，哪怕开仓不足时间止损窗口。"""
    monkeypatch.setattr(C, "ACCOUNT_FILE", tmp_path / "account.json")
    monkeypatch.setattr(C, "DATA_DIR", tmp_path)
    monkeypatch.setattr(C, "DAILY_TRADES_FILE", tmp_path / "daily.jsonl")
    monkeypatch.setattr(C, "TRADES_FILE", tmp_path / "trades.jsonl")
    monkeypatch.setattr(C, "HARD_STOP_PCT", 0.25)
    monkeypatch.setattr(C, "TIME_STOP_MINUTES", 25.0)

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


def test_tp1_at_plus_18(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "ACCOUNT_FILE", tmp_path / "account.json")
    monkeypatch.setattr(C, "DATA_DIR", tmp_path)
    monkeypatch.setattr(C, "DAILY_TRADES_FILE", tmp_path / "daily.jsonl")
    monkeypatch.setattr(C, "TRADES_FILE", tmp_path / "trades.jsonl")
    monkeypatch.setattr(C, "TP1_PCT", 0.18)
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
    events = broker.manage({mint: 1.19})  # 达到 +18% 阈值（留一点浮点余量）
    assert any(e["type"] == "tp1" for e in events)
    assert broker.positions[mint]["tp1_done"] is True
    assert broker.positions[mint]["qty_left"] == pytest.approx(0.45)


def _time_stop_pos(mint: str) -> dict:
    return {
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


def test_time_stop_closes_losing_position(tmp_path, monkeypatch):
    """方案B①：满时间窗且浮亏 → 强制时间止损清仓。"""
    monkeypatch.setattr(C, "ACCOUNT_FILE", tmp_path / "account.json")
    monkeypatch.setattr(C, "DATA_DIR", tmp_path)
    monkeypatch.setattr(C, "DAILY_TRADES_FILE", tmp_path / "daily.jsonl")
    monkeypatch.setattr(C, "TRADES_FILE", tmp_path / "trades.jsonl")

    broker = PaperBroker()
    broker.dry_run = True
    mint = "TimeLoser"
    broker.positions[mint] = _time_stop_pos(mint)
    events = broker.manage({mint: 0.92})  # -8%，未及硬止损但已到时间窗
    assert [event["type"] for event in events] == ["time_stop"]
    assert mint not in broker.positions


def test_profit_exempts_time_stop(tmp_path, monkeypatch):
    """方案B②：满时间窗但浮盈 → 豁免时间止损、硬止损上移保本、交移动止盈追踪。"""
    monkeypatch.setattr(C, "ACCOUNT_FILE", tmp_path / "account.json")
    monkeypatch.setattr(C, "DATA_DIR", tmp_path)
    monkeypatch.setattr(C, "DAILY_TRADES_FILE", tmp_path / "daily.jsonl")
    monkeypatch.setattr(C, "TRADES_FILE", tmp_path / "trades.jsonl")

    broker = PaperBroker()
    broker.dry_run = True
    mint = "TimeWinner"
    broker.positions[mint] = _time_stop_pos(mint)
    events = broker.manage({mint: 1.05})  # +5% 浮盈
    assert any(e["type"] == "be_takeover" for e in events)
    assert "time_stop" not in [e["type"] for e in events]
    assert mint in broker.positions  # 未被平仓
    pos = broker.positions[mint]
    assert pos["time_exempt"] is True
    assert pos["be_takeover"] is True
    assert pos["be_price"] == pytest.approx(1.0)

    # 后续回落跌破保本价 → 保本止损清仓（be_stop），且时间止损不再触发
    events2 = broker.manage({mint: 0.99})
    assert any(e["type"] == "be_stop" for e in events2)
    assert mint not in broker.positions
