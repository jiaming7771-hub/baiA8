"""影子模式：真价虚拟成交 + 四层出场报告。

注意：本测试会改环境变量并 reload config，结束后恢复，避免污染其他测试。
"""

from __future__ import annotations

import importlib
import os

import pytest

_ENV_OVERRIDES = {
    "SHADOW_MODE": "true",
    "SHADOW_SIZE_SOL": "1.0",
    "SHADOW_SLIPPAGE_BPS": "300",
    "PUMP_DEMO_SCAN": "0",
    "PUMP_DRY_RUN": "1",
    "PUMP_HARD_STOP_PCT": "0.25",
    "PUMP_TP1_PCT": "0.18",
    "PUMP_TRAIL_DD": "0.13",
    "PUMP_TIME_STOP": "25",
}


@pytest.fixture(autouse=True)
def _shadow_env(tmp_path, monkeypatch):
    import pumpfun.config as C

    saved = {k: os.environ.get(k) for k in _ENV_OVERRIDES}
    os.environ.update(_ENV_OVERRIDES)
    importlib.reload(C)
    monkeypatch.setattr(C, "SHADOW_TRADES_FILE", tmp_path / "shadow_trades.jsonl")
    monkeypatch.setattr(C, "SHADOW_SUMMARY_FILE", tmp_path / "shadow_summary.json")
    monkeypatch.setattr(C, "ACCOUNT_FILE", tmp_path / "account.json")
    monkeypatch.setattr(C, "DAILY_TRADES_FILE", tmp_path / "daily.jsonl")
    monkeypatch.setattr(C, "TRADES_FILE", tmp_path / "trades.jsonl")
    monkeypatch.setattr(C, "EXEC_LOG_FILE", tmp_path / "execution.log")
    shadow_report._open_book.clear()
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(C)


import pumpfun.config as C

from pumpfun.execution import PaperBroker
from pumpfun import shadow_report


def _sig(mint: str, price: float) -> dict:
    return {
        "mint": mint,
        "symbol": "TEST",
        "price": price,
        "score": 99,
        "ath_drop_pct": 85,
        "panic_ratio": 3.0,
        "whale_dump_pct": 80,
        "tx_count_m5": 12,
        "volume_m5_sol": 3.0,
        "age_minutes": 60,
        "hard_pass": True,
    }


def test_shadow_hard_stop():
    b = PaperBroker()
    b.shadow = True
    b.dry_run = True
    pos = b.open_long(_sig("mintHS", 1.0))
    assert pos and pos["shadow"] is True
    assert abs(pos["sol_spent"] - 1.0) < 1e-9
    # -25% → hard stop
    events = b.manage({"mintHS": 0.74})
    assert any(e["type"] == "hard_stop" for e in events)
    assert "mintHS" not in b.positions


def test_shadow_tp1_and_trail():
    b = PaperBroker()
    b.shadow = True
    b.dry_run = True
    pos = b.open_long(_sig("mintTP", 1.0))
    assert pos
    # +28% → TP1
    events = b.manage({"mintTP": 1.28})
    assert any(e["type"] == "tp1" for e in events)
    assert "mintTP" in b.positions
    assert b.positions["mintTP"]["tp1_done"] is True
    # 推高峰值再回撤 13%
    b.manage({"mintTP": 1.50})
    peak = float(b.positions["mintTP"]["peak"])
    line = peak * (1.0 - C.TRAIL_DRAWDOWN)
    events = b.manage({"mintTP": line * 0.99})
    assert any(e["type"] == "trail_stop" for e in events)
    assert "mintTP" not in b.positions


def test_shadow_time_stop():
    import time

    b = PaperBroker()
    b.shadow = True
    b.dry_run = True
    pos = b.open_long(_sig("mintTM", 1.0))
    assert pos
    pos["opened_at"] = time.time() - (C.TIME_STOP_MINUTES + 0.5) * 60
    # 峰值已达标 → 跳过 dead_stop，专测时间止损路径
    pos["peak"] = 1.0 * (1.0 + float(C.DEAD_CUT_MIN_PNL) + 0.02)
    # 浮亏盘满时间窗 → 时间止损（浮盈盘会被方案B豁免，故这里用亏损价）
    events = b.manage({"mintTM": 0.90})
    assert any(e["type"] == "time_stop" for e in events)


def test_shadow_report_summary_keys():
    s = shadow_report.get_summary()
    assert "win_rate" in s
    assert "trades" in s
    assert "rules" in s


def test_shadow_pnl_rebuilds_from_trades_on_restart(tmp_path, monkeypatch):
    """重启后账户若被清零，须从 shadow_trades 重建，避免页面收益率归零。"""
    import json

    rows = [
        {"closed_at": "2026-07-26T01:00:00+00:00", "pnl_sol": 0.12, "symbol": "A"},
        {"closed_at": "2026-07-26T01:10:00+00:00", "pnl_sol": -0.04, "symbol": "B"},
    ]
    C.SHADOW_TRADES_FILE.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )
    # 模拟「刷新后账户被写成空本金」
    C.ACCOUNT_FILE.write_text(
        json.dumps(
            {
                "bankroll_sol": 10.0,
                "cash_sol": 10.0,
                "gross_realized_sol": 0.0,
                "total_fees_sol": 0.0,
                "total_slippage_sol": 0.0,
                "total_gas_sol": 0.0,
                "realized_pnl_sol": 0.0,
                "open_positions": 0,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(C, "SHADOW_MODE", True)
    b = PaperBroker()
    assert b.shadow is True
    assert b.net_realized() == pytest.approx(0.08)
    assert b.cash == pytest.approx(b.bankroll + 0.08)
    stats = shadow_report.stats_for_ui(b.bankroll, equity=b.cash, unrealized_pnl=0.0)
    assert stats["total_pnl_sol"] == pytest.approx(0.08)
    assert stats["exit_count"] == 2
