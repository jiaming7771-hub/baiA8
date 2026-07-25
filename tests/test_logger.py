"""24h 交易日志持久化与统计准确性。"""

from __future__ import annotations

from pathlib import Path

import pytest

from pumpfun import config as C
from pumpfun import journal


@pytest.fixture()
def isolated_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data = tmp_path / "data"
    logs = tmp_path / "trading_logs"
    data.mkdir()
    logs.mkdir()
    monkeypatch.setattr(C, "DATA_DIR", data)
    monkeypatch.setattr(C, "TRADING_LOGS_DIR", logs)
    monkeypatch.setattr(C, "DAILY_TRADES_FILE", data / "daily_trades.jsonl")
    monkeypatch.setattr(C, "TRADES_FILE", data / "trades.jsonl")
    monkeypatch.setattr(C, "EXEC_LOG_FILE", logs / "bot_execution.log")
    # 清空残留
    journal.clear_trades()
    yield
    journal.clear_trades()


class TestJournal24h:
    def test_record_and_stats(self, isolated_journal):
        bankroll = 10.0
        # 10 笔买入 + 若干出场
        for i in range(10):
            journal.record_trade(
                action="buy",
                mint=f"mint_{i}_pump",
                symbol=f"COIN{i}",
                amount_sol=0.04,
                price=1e-6 * (i + 1),
                dry_run=True,
                metrics={"panic_ratio": 3.0, "ath_drop_pct": 85, "spread_pct": 5},
            )
        # 6 胜 4 负的出场腿
        for i in range(6):
            journal.record_trade(
                action="tp1",
                mint=f"mint_{i}_pump",
                symbol=f"COIN{i}",
                amount_sol=0.022,
                price=1.3e-6 * (i + 1),
                pnl_sol=0.005,
                pnl_percent=28.0,
                dry_run=True,
            )
        for i in range(6, 10):
            journal.record_trade(
                action="time_stop",
                mint=f"mint_{i}_pump",
                symbol=f"COIN{i}",
                amount_sol=0.03,
                price=0.9e-6 * (i + 1),
                pnl_sol=-0.002,
                pnl_percent=-10.0,
                dry_run=True,
            )

        stats = journal.compute_stats_24h(bankroll)
        assert stats["total_trades"] == 10
        assert stats["exit_count"] == 10
        assert stats["win_count"] == 6
        assert stats["loss_count"] == 4
        assert stats["win_rate"] == 60.0
        expected_legs = 6 * 0.005 + 4 * (-0.002)
        # 无 equity 时退化到流水加总
        assert stats["pnl_method"] == "legs_sum_fallback"
        assert abs(stats["total_pnl_sol"] - expected_legs) < 1e-9
        assert abs(stats["legs_pnl_sol"] - expected_legs) < 1e-9

        # 资产净值法：总盈亏 = equity - bankroll（与右下角对齐）
        equity = bankroll + 0.1234
        nav = journal.compute_stats_24h(
            bankroll, equity=equity, realized_pnl=0.10, unrealized_pnl=0.0234
        )
        assert nav["pnl_method"] == "nav_equity"
        assert abs(nav["total_pnl_sol"] - 0.1234) < 1e-9
        assert abs(nav["total_pnl_pct"] - 1.23) < 1e-6  # round(..., 2)
        # 流水加总仍保留对照字段，可与净值不一致
        assert abs(nav["legs_pnl_sol"] - expected_legs) < 1e-9

        trades = journal.load_trades(hours=24, limit=50)
        assert len(trades) == 20
        assert C.EXEC_LOG_FILE.exists()
        assert C.EXEC_LOG_FILE.read_text(encoding="utf-8").count("\n") >= 20

        csv_text = journal.trades_to_csv(hours=24)
        assert "timestamp" in csv_text
        assert "COIN0" in csv_text

    def test_clear_trades(self, isolated_journal):
        journal.record_trade(
            action="buy",
            mint="m1",
            symbol="X",
            amount_sol=0.04,
            price=1e-6,
        )
        assert journal.load_trades(hours=24)
        journal.clear_trades()
        assert journal.load_trades(hours=24) == []
        stats = journal.compute_stats_24h(10)
        assert stats["total_trades"] == 0
        assert stats["win_rate"] == 0.0
