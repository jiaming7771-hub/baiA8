"""评分分轨出场 + pre_tp1 金库确认 + 假突破早砍。"""

from __future__ import annotations

import time

import pytest

from backend.pumpfun import config as C
from backend.pumpfun.execution import PaperBroker, resolve_exit_tier


def _broker(tmp_path, monkeypatch) -> PaperBroker:
    monkeypatch.setattr(C, "ACCOUNT_FILE", tmp_path / "account.json")
    monkeypatch.setattr(C, "DATA_DIR", tmp_path)
    monkeypatch.setattr(C, "DAILY_TRADES_FILE", tmp_path / "daily.jsonl")
    monkeypatch.setattr(C, "TRADES_FILE", tmp_path / "trades.jsonl")
    monkeypatch.setattr(C, "EXIT_TIER_ENABLED", True)
    monkeypatch.setattr(C, "EXIT_PREMIUM_MIN_SCORE", 70.0)
    monkeypatch.setattr(C, "EXIT_NORMAL_TP_PCT", 0.35)
    monkeypatch.setattr(C, "PRE_TP1_SCALE_ENABLED", True)
    monkeypatch.setattr(C, "PRE_TP1_SCALE_LOSS", 0.18)
    monkeypatch.setattr(C, "PRE_TP1_SCALE_SELL", 0.50)
    monkeypatch.setattr(C, "PRE_TP1_REQUIRE_VAULT", True)
    monkeypatch.setattr(C, "PRE_TP1_VAULT_DROP", 0.20)
    monkeypatch.setattr(C, "TRACK_A_TP1", 0.25)
    monkeypatch.setattr(C, "TRACK_A_TP1_SELL", 0.30)
    monkeypatch.setattr(C, "TRACK_A_HARD_STOP", 0.20)
    monkeypatch.setattr(C, "FAILED_BREAKOUT_ENABLED", True)
    monkeypatch.setattr(C, "FAILED_BREAKOUT_PEAK_PCT", 0.18)
    monkeypatch.setattr(C, "FAILED_BREAKOUT_GIVEBACK_PNL", -0.02)
    b = PaperBroker()
    b.dry_run = True
    b.cash = 10.0
    return b


def _pos(mint: str, *, score: float, exit_tier: str | None = None) -> dict:
    return {
        "id": "t1",
        "mint": mint,
        "symbol": "TIER",
        "entry": 1.0,
        "entry_mark": 1.0,
        "qty": 1.0,
        "qty_left": 1.0,
        "sol_spent": 1.0,
        "opened_at": time.time(),
        "peak": 1.0,
        "tp1_done": False,
        "tp2_done": False,
        "tp3_done": False,
        "pre_tp1_scale_done": False,
        "trail_line": None,
        "track": "A",
        "dry_run": True,
        "score": score,
        "exit_tier": exit_tier or resolve_exit_tier(score=score),
        "entry_sol_vault": 100.0,
        "sol_vault": 100.0,
        "fees_sol": 0.0,
        "gas_sol": 0.0,
        "slippage_sol": 0.0,
        "slippage_bps": 500,
        "max_float_pnl_pct": None,
    }


def test_resolve_exit_tier_by_score(monkeypatch):
    monkeypatch.setattr(C, "EXIT_TIER_ENABLED", True)
    monkeypatch.setattr(C, "EXIT_PREMIUM_MIN_SCORE", 70.0)
    assert resolve_exit_tier(score=69.9) == "normal"
    assert resolve_exit_tier(score=70.0) == "premium"
    assert resolve_exit_tier(score=None) == "premium"


def test_resolve_exit_tier_disabled_is_normal(monkeypatch):
    monkeypatch.setattr(C, "EXIT_TIER_ENABLED", False)
    assert resolve_exit_tier(score=99.0) == "normal"
    assert resolve_exit_tier(score=None) == "normal"


def test_normal_tier_full_clear_at_plus_35(tmp_path, monkeypatch):
    broker = _broker(tmp_path, monkeypatch)
    mint = "NormalMint"
    broker.positions[mint] = _pos(mint, score=60.0)
    events = broker.manage({mint: 1.36})
    assert any(e["type"] == "tier_tp" for e in events)
    assert mint not in broker.positions


def test_early_track_full_clear_at_plus_23(tmp_path, monkeypatch):
    """E 轨 +23% 一次清完，不等普通档 35%。"""
    broker = _broker(tmp_path, monkeypatch)
    monkeypatch.setattr(C, "EXIT_TIER_ENABLED", False)
    monkeypatch.setattr(C, "TRACK_E_TP_PCT", 0.23)
    mint = "EarlyMint"
    pos = _pos(mint, score=64.0)
    pos["track"] = "E"
    pos["exit_tier"] = "normal"
    broker.positions[mint] = pos
    # +22% 不触发；+24% 触发（避开 1.23 浮点边界）
    events_wait = broker.manage({mint: 1.22})
    assert not any(e["type"] == "tier_tp" for e in events_wait)
    assert mint in broker.positions
    events = broker.manage({mint: 1.24})
    assert any(e["type"] == "tier_tp" and e.get("track") == "E" for e in events)
    assert mint not in broker.positions


def test_normal_tier_not_yet_at_25(tmp_path, monkeypatch):
    """+25% 不再全清；普通档要到 +35%。"""
    broker = _broker(tmp_path, monkeypatch)
    mint = "NormalWait"
    broker.positions[mint] = _pos(mint, score=60.0)
    events = broker.manage({mint: 1.26})
    assert not any(e["type"] == "tier_tp" for e in events)
    assert mint in broker.positions


def test_premium_tier_still_partial_tp1(tmp_path, monkeypatch):
    broker = _broker(tmp_path, monkeypatch)
    mint = "PremiumMint"
    broker.positions[mint] = _pos(mint, score=74.0)
    events = broker.manage({mint: 1.26})
    assert any(e["type"] == "tp1" for e in events)
    assert mint in broker.positions
    assert broker.positions[mint]["tp1_done"] is True
    assert broker.positions[mint]["qty_left"] == pytest.approx(0.70)


def test_failed_breakout_fires_after_peak(tmp_path, monkeypatch):
    broker = _broker(tmp_path, monkeypatch)
    mint = "FailBO"
    pos = _pos(mint, score=83.0)
    pos["max_float_pnl_pct"] = 22.0  # 曾 +22%
    pos["peak"] = 1.22
    broker.positions[mint] = pos
    events = broker.manage({mint: 0.97})  # now -3%
    assert any(e["type"] == "failed_breakout" for e in events)
    assert mint not in broker.positions


def test_failed_breakout_skips_without_peak(tmp_path, monkeypatch):
    broker = _broker(tmp_path, monkeypatch)
    mint = "NoPeak"
    pos = _pos(mint, score=83.0)
    pos["max_float_pnl_pct"] = 10.0  # 未到 +18%
    broker.positions[mint] = pos
    events = broker.manage({mint: 0.97})
    assert not any(e["type"] == "failed_breakout" for e in events)
    assert mint in broker.positions


def test_pre_tp1_skipped_without_vault_drop(tmp_path, monkeypatch):
    broker = _broker(tmp_path, monkeypatch)
    mint = "FakeDump"
    pos = _pos(mint, score=60.0)
    pos["sol_vault"] = 95.0  # only -5%, need 20%
    broker.positions[mint] = pos
    events = broker.manage({mint: 0.80})  # -20% price → hard stop may fire
    # at -20% with HARD_STOP 0.20, hard_stop may arm/fire; pre_tp1 must not
    assert not any(e["type"] == "pre_tp1_scale" for e in events)


def test_pre_tp1_fires_with_vault_drop(tmp_path, monkeypatch):
    broker = _broker(tmp_path, monkeypatch)
    # keep above hard stop so only pre_tp1 fires
    monkeypatch.setattr(C, "TRACK_A_HARD_STOP", 0.35)
    mint = "RealDump"
    pos = _pos(mint, score=60.0)
    pos["sol_vault"] = 75.0  # -25% vault
    broker.positions[mint] = pos
    events = broker.manage({mint: 0.80})  # -20% price
    assert any(e["type"] == "pre_tp1_scale" for e in events)
    assert broker.positions[mint]["pre_tp1_scale_done"] is True
    assert broker.positions[mint]["qty_left"] == pytest.approx(0.50)


def test_premium_hard_stop_wider_than_normal(tmp_path, monkeypatch):
    """普通 -20% 可砍；优质同价仍留着，到 -28% 才硬止。"""
    broker = _broker(tmp_path, monkeypatch)
    monkeypatch.setattr(C, "EXIT_PREMIUM_HARD_STOP", 0.28)
    monkeypatch.setattr(C, "HARD_STOP_CONFIRM_TICKS", 1)
    monkeypatch.setattr(C, "HARD_STOP_CONFIRM_SEC", 0.0)
    monkeypatch.setattr(C, "PANIC_STOP_PCT", 0.45)

    mint_n = "NormHS"
    broker.positions[mint_n] = _pos(mint_n, score=60.0)
    # arm then fire: first tick arms, need 2 manages if confirm ticks=1 and sec=0
    broker.manage({mint_n: 0.79})  # -21%
    events_n = broker.manage({mint_n: 0.79})
    assert any(e["type"] == "hard_stop" for e in events_n) or mint_n not in broker.positions

    mint_p = "PremHS"
    broker.positions[mint_p] = _pos(mint_p, score=83.0)
    broker.manage({mint_p: 0.79})  # -21% — should NOT hard stop yet
    events_p = broker.manage({mint_p: 0.79})
    assert not any(e["type"] == "hard_stop" for e in events_p)
    assert mint_p in broker.positions

    broker.manage({mint_p: 0.71})  # -29%
    events_p2 = broker.manage({mint_p: 0.71})
    assert any(e["type"] == "hard_stop" for e in events_p2) or mint_p not in broker.positions
