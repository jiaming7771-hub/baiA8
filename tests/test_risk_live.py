"""实盘硬风控单测：滑点 / 仓位 / 回撤与绝对亏损熔断 / 四层出场。"""

from __future__ import annotations

import time

import pytest

from pumpfun import config as C
from pumpfun.execution import PaperBroker
from pumpfun.risk import RiskBlocked, RiskGuard


def test_slippage_clamped_to_hard_bounds():
    g = RiskGuard()
    # HARD_MIN=100：入场可走 250bps；旧 HARD_MIN=500 会把一切抬到 5%
    assert g.clamp_slippage_bps(50) == 100
    assert g.clamp_slippage_bps(100) == 100
    assert g.clamp_slippage_bps(250) == 250
    assert g.clamp_slippage_bps(500) == 500
    assert g.clamp_slippage_bps(800) == 800
    assert g.clamp_slippage_bps(5000) == 1000  # 超过 10% → 砍到 10%
    assert C.ENTRY_MAX_SLIPPAGE_BPS == 250
    assert C.ENTRY_MAX_SLIPPAGE_BPS <= C.MAX_SLIPPAGE_BPS


def test_urgent_slippage_can_reach_30_percent():
    g = RiskGuard()
    assert g.clamp_slippage_bps(5000, urgent=True) == C.URGENT_SLIPPAGE_BPS_MAX
    assert g.clamp_slippage_bps(2500, urgent=True) == 2500
    assert C.URGENT_SLIPPAGE_BPS_MAX >= 2000


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
    """出场口径按「方案②极贪跟波」：A/B 一致 -35% / TP1+60% 卖 20% / 回撤 18%。"""
    assert C.STRATEGY_MODE == "momentum"
    assert C.IS_MOMENTUM is True
    assert C.TRACK_B_ENABLED is True
    assert C.TRACK_A_HARD_STOP == pytest.approx(0.35)
    assert C.TRACK_A_TP1 == pytest.approx(0.60)
    assert C.TRACK_A_TP1_SELL == pytest.approx(0.20)
    assert C.TRACK_A_TRAIL == pytest.approx(0.18)
    assert C.TRACK_B_HARD_STOP == pytest.approx(0.35)
    assert C.TRACK_B_TP1 == pytest.approx(0.60)
    assert C.TRACK_B_TP1_SELL == pytest.approx(0.20)
    assert C.TRACK_B_TRAIL == pytest.approx(0.18)
    assert C.HARD_STOP_PCT == pytest.approx(0.35)
    assert C.TP1_PCT == pytest.approx(0.60)
    # 止损二次确认 + 崩塌立即逃生 + 买前确认
    assert C.HARD_STOP_CONFIRM_SEC == pytest.approx(6.0)
    assert C.HARD_STOP_CONFIRM_TICKS == 2
    assert C.PANIC_STOP_PCT == pytest.approx(0.45)
    assert C.ENTRY_CONFIRM_SEC == pytest.approx(8.0)
    assert C.ENTRY_CHG_M5_MIN == pytest.approx(3.0)
    assert C.ENTRY_CHG_M5_MAX == pytest.approx(25.0)
    assert C.EXIT_COOLDOWN_SEC == pytest.approx(1800.0)
    assert C.REENTRY_STRONG_SEC == pytest.approx(600.0)
    assert C.REENTRY_MAX_RETRY == 1
    assert C.URGENT_SLIPPAGE_BPS_MAX == 3000
    assert C.DRAWDOWN_HALT == pytest.approx(0.15)
    assert C.ABS_LOSS_HALT_SOL == pytest.approx(0.6)
    assert C.MAX_OPEN_POSITIONS == 3
    assert C.DEAD_CUT_SECONDS == pytest.approx(105.0)
    assert C.DEAD_CUT_MIN_PNL == pytest.approx(0.03)


def test_reject_deep_pullback_not_momentum(monkeypatch):
    """距高点回撤 > A 轨 20% 不算主升攻击区，必须拒。"""
    from pumpfun.strategy import Candidate, pass_hard_filters
    import time as _t

    # 本机 .env 常年放宽回撤上限，这里钉回代码默认值以测过滤逻辑本身
    monkeypatch.setattr(C, "TRACK_A_PULLBACK_MAX", 0.20)
    monkeypatch.setattr(C, "TRACK_B_AGE_MIN", 120.0)

    deep = Candidate(
        mint="deep", symbol="DEEP", listed_at=_t.time() - 30 * 60,
        ath_price=1.0, price=0.78,  # 回撤 22%
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


def _hard_stop_broker(tmp_path, monkeypatch, mint: str) -> PaperBroker:
    monkeypatch.setattr(C, "ACCOUNT_FILE", tmp_path / "account.json")
    monkeypatch.setattr(C, "DATA_DIR", tmp_path)
    monkeypatch.setattr(C, "DAILY_TRADES_FILE", tmp_path / "daily.jsonl")
    monkeypatch.setattr(C, "TRADES_FILE", tmp_path / "trades.jsonl")
    monkeypatch.setattr(C, "HARD_STOP_PCT", 0.35)
    monkeypatch.setattr(C, "TRACK_A_HARD_STOP", 0.35)
    monkeypatch.setattr(C, "PANIC_STOP_PCT", 0.45)
    monkeypatch.setattr(C, "HARD_STOP_CONFIRM_TICKS", 2)
    monkeypatch.setattr(C, "HARD_STOP_CONFIRM_SEC", 6.0)

    broker = PaperBroker()
    broker.dry_run = True
    broker.cash = 10.0
    broker.positions[mint] = {
        "id": "hs1",
        "mint": mint,
        "symbol": "HST",
        "entry": 1.0,
        "qty": 0.03,
        "qty_left": 0.03,
        "sol_spent": 0.03,
        "opened_at": time.time() - 60,
        "peak": 1.0,
        "tp1_done": False,
        "trail_line": None,
        "dead_cut_done": True,
        "track": "A",
        "dry_run": True,
        "fees_sol": 0.0,
        "gas_sol": 0.0,
        "slippage_sol": 0.0,
        "slippage_bps": 500,
    }
    return broker


def test_hard_stop_needs_confirmation(tmp_path, monkeypatch):
    """破 -35% 先进警戒，满足 2 次报价 + 6 秒仍破线才斩仓。"""
    mint = "TestMintHardStop"
    broker = _hard_stop_broker(tmp_path, monkeypatch, mint)

    events = broker.manage({mint: 0.60})  # -40%，首次破线只警戒
    assert events == []
    assert mint in broker.positions
    assert broker.positions[mint]["stop_arm_ticks"] == 1

    # 倒推警戒起点，模拟已持续 6 秒
    broker.positions[mint]["stop_arm_ts"] = time.time() - 7
    events = broker.manage({mint: 0.60})
    assert any(e["type"] == "hard_stop" for e in events)
    assert mint not in broker.positions


def test_hard_stop_warning_cleared_on_recovery(tmp_path, monkeypatch):
    """警戒期内价格拉回止损线上方 → 撤销警戒，不砍仓。"""
    mint = "TestMintRecover"
    broker = _hard_stop_broker(tmp_path, monkeypatch, mint)

    broker.manage({mint: 0.60})
    assert broker.positions[mint].get("stop_arm_ts")

    events = broker.manage({mint: 0.80})  # -20%，收回止损线上方
    assert not any(e["type"] == "hard_stop" for e in events)
    assert "stop_arm_ts" not in broker.positions[mint]
    assert mint in broker.positions


def test_panic_stop_skips_confirmation(tmp_path, monkeypatch):
    """跌破 -45% 崩塌线 → 不等确认，单次报价立即清仓。"""
    mint = "TestMintPanic"
    broker = _hard_stop_broker(tmp_path, monkeypatch, mint)

    events = broker.manage({mint: 0.50})  # -50%
    assert any(e["type"] == "hard_stop" for e in events)
    assert mint not in broker.positions


def test_tp1_at_plus_18(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "ACCOUNT_FILE", tmp_path / "account.json")
    monkeypatch.setattr(C, "DATA_DIR", tmp_path)
    monkeypatch.setattr(C, "DAILY_TRADES_FILE", tmp_path / "daily.jsonl")
    monkeypatch.setattr(C, "TRADES_FILE", tmp_path / "trades.jsonl")
    monkeypatch.setattr(C, "TRACK_A_TP1", 0.18)
    monkeypatch.setattr(C, "TRACK_A_TP1_SELL", 0.55)
    monkeypatch.setattr(C, "TRACK_A_HARD_STOP", 0.25)
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
        "track": "A",
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
        "opened_at": time.time() - (C.TRACK_A_TIME_STOP + 0.1) * 60,
        "peak": 1.05,  # 曾有过浮盈，避免先被 dead_stop 截胡
        "tp1_done": False,
        "trail_line": None,
        "dead_cut_done": True,
        "track": "A",
        "dry_run": True,
        "fees_sol": 0.0,
        "gas_sol": 0.0,
        "slippage_sol": 0.0,
        "slippage_bps": 500,
    }


def test_time_stop_disabled_keeps_losing_position(tmp_path, monkeypatch):
    """时间止损已下线：超时且浮亏也不砍仓，仅由硬止损/移动止盈接管。"""
    monkeypatch.setattr(C, "ACCOUNT_FILE", tmp_path / "account.json")
    monkeypatch.setattr(C, "DATA_DIR", tmp_path)
    monkeypatch.setattr(C, "DAILY_TRADES_FILE", tmp_path / "daily.jsonl")
    monkeypatch.setattr(C, "TRADES_FILE", tmp_path / "trades.jsonl")

    broker = PaperBroker()
    broker.dry_run = True
    mint = "TimeLoser"
    broker.positions[mint] = _time_stop_pos(mint)
    events = broker.manage({mint: 0.92})  # -8%，远未及 -35% 硬止损
    assert [e["type"] for e in events if e["type"] in ("time_stop", "be_takeover")] == []
    assert mint in broker.positions


def test_time_stop_disabled_keeps_winning_position(tmp_path, monkeypatch):
    """超时浮盈同样不转保本：贪多方案下只认峰值回撤。"""
    monkeypatch.setattr(C, "ACCOUNT_FILE", tmp_path / "account.json")
    monkeypatch.setattr(C, "DATA_DIR", tmp_path)
    monkeypatch.setattr(C, "DAILY_TRADES_FILE", tmp_path / "daily.jsonl")
    monkeypatch.setattr(C, "TRADES_FILE", tmp_path / "trades.jsonl")

    broker = PaperBroker()
    broker.dry_run = True
    mint = "TimeWinner"
    broker.positions[mint] = _time_stop_pos(mint)
    events = broker.manage({mint: 1.05})  # +5% 浮盈
    assert [e["type"] for e in events if e["type"] in ("time_stop", "be_takeover")] == []
    assert mint in broker.positions
    assert not broker.positions[mint].get("be_takeover")


def test_dead_stop_cuts_zombie_after_window(tmp_path, monkeypatch):
    """开仓约 105s 内峰值浮盈 < +3% 且成交枯竭 → dead_stop。"""
    monkeypatch.setattr(C, "ACCOUNT_FILE", tmp_path / "account.json")
    monkeypatch.setattr(C, "DATA_DIR", tmp_path)
    monkeypatch.setattr(C, "DAILY_TRADES_FILE", tmp_path / "daily.jsonl")
    monkeypatch.setattr(C, "TRADES_FILE", tmp_path / "trades.jsonl")
    monkeypatch.setattr(C, "IS_MOMENTUM", True)
    monkeypatch.setattr(C, "DEAD_CUT_SECONDS", 105.0)
    monkeypatch.setattr(C, "DEAD_CUT_MIN_PNL", 0.03)

    broker = PaperBroker()
    broker.dry_run = True
    mint = "Zombie"
    broker.positions[mint] = {
        "id": "dead1",
        "mint": mint,
        "symbol": "ZOMB",
        "entry": 1.0,
        "qty": 1.0,
        "qty_left": 1.0,
        "sol_spent": 1.0,
        "opened_at": time.time() - 110,
        "peak": 1.01,  # 峰值仅 +1%
        "tp1_done": False,
        "trail_line": None,
        "dry_run": True,
        "volume_m5_sol": 10.0,
        "fees_sol": 0.0,
        "gas_sol": 0.0,
        "slippage_sol": 0.0,
        "slippage_bps": 500,
    }
    events = broker.manage({mint: 1.005})
    assert any(e["type"] == "dead_stop" for e in events)
    assert mint not in broker.positions


def test_dead_stop_skips_if_already_pumped(tmp_path, monkeypatch):
    """峰值已超过 +3% 则不触发死盘早砍。"""
    monkeypatch.setattr(C, "ACCOUNT_FILE", tmp_path / "account.json")
    monkeypatch.setattr(C, "DATA_DIR", tmp_path)
    monkeypatch.setattr(C, "DAILY_TRADES_FILE", tmp_path / "daily.jsonl")
    monkeypatch.setattr(C, "TRADES_FILE", tmp_path / "trades.jsonl")
    monkeypatch.setattr(C, "IS_MOMENTUM", True)
    monkeypatch.setattr(C, "DEAD_CUT_SECONDS", 105.0)
    monkeypatch.setattr(C, "DEAD_CUT_MIN_PNL", 0.03)

    broker = PaperBroker()
    broker.dry_run = True
    mint = "Alive"
    broker.positions[mint] = {
        "id": "alive1",
        "mint": mint,
        "symbol": "ALIVE",
        "entry": 1.0,
        "qty": 1.0,
        "qty_left": 1.0,
        "sol_spent": 1.0,
        "opened_at": time.time() - 110,
        "peak": 1.08,  # 曾到 +8%
        "tp1_done": False,
        "trail_line": None,
        "dry_run": True,
        "volume_m5_sol": 10.0,
        "fees_sol": 0.0,
        "gas_sol": 0.0,
        "slippage_sol": 0.0,
        "slippage_bps": 500,
    }
    events = broker.manage({mint: 1.02})
    assert not any(e["type"] == "dead_stop" for e in events)
    assert mint in broker.positions
