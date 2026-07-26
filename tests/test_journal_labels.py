"""交易日志标签必须反映**当时**生效的阈值，而不是复盘时的配置。

事故背景：ACTION_LABELS 在模块导入时用 C.HARD_STOP_PCT / C.TP1_PCT 拼好字符串。
阈值是常调的（同一份历史里 hard_stop 出现过 -13% / -25% / -35%，TP1 出现过
+22% / +25%），拿当下配置渲染历史 → 复盘看到的止损线根本不是那笔的止损线。
更隐蔽的是：全局 C.TP1_PCT 跟真正驱动出场的轨道阈值 TRACK_A_TP1 本来就不是
同一个数，所以即使一次都不改配置，标签也是错的。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pumpfun import config as C
from pumpfun import journal
from pumpfun.execution import PaperBroker, _exit_params, _fired_threshold


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
    journal.clear_trades()
    yield
    journal.clear_trades()


class TestLabelRendering:
    def test_label_uses_supplied_threshold(self):
        assert journal.action_label("hard_stop", threshold_pct=0.13) == "价格硬止损(-13%)"
        assert journal.action_label("hard_stop", threshold_pct=0.35) == "价格硬止损(-35%)"
        assert journal.action_label("tp1", threshold_pct=0.22) == "第一止盈(+22%)"

    def test_reason_uses_supplied_threshold(self):
        got = journal.exit_reason_text("tp1", threshold_pct=0.25, sell_ratio=0.5)
        assert "+25%" in got and "50%" in got
        assert "-13%" in journal.exit_reason_text("hard_stop", threshold_pct=0.13)
        assert "≥9%" in journal.exit_reason_text("trail_stop", threshold_pct=0.09)

    def test_missing_threshold_never_invents_a_number(self):
        """老记录没存阈值时宁可不写数字，也不能补一个当下配置的数字。"""
        for action in ("hard_stop", "tp1"):
            label = journal.action_label(action)
            assert not any(ch.isdigit() for ch in label), label
            reason = journal.exit_reason_text(action)
            assert not any(ch.isdigit() for ch in reason), reason

    def test_label_is_independent_of_current_config(self, monkeypatch):
        """改配置不得改变已给定阈值的渲染结果。"""
        before = journal.action_label("hard_stop", threshold_pct=0.13)
        monkeypatch.setattr(C, "HARD_STOP_PCT", 0.99)
        monkeypatch.setattr(C, "TRACK_A_HARD_STOP", 0.99)
        assert journal.action_label("hard_stop", threshold_pct=0.13) == before

    def test_time_stop_threshold_is_minutes_not_percent(self):
        assert "12分钟" in journal.exit_reason_text("time_stop", threshold_pct=12)


class TestPersistedThreshold:
    def test_record_persists_threshold_and_basis(self, isolated_journal):
        journal.record_trade(
            action="hard_stop",
            mint="m1",
            symbol="OLD",
            amount_sol=0.02,
            price=8.7e-7,
            pnl_sol=-0.006,
            pnl_percent=-13.0,
            threshold_pct=0.13,
            basis_price=1e-6,
        )
        row = json.loads(C.DAILY_TRADES_FILE.read_text(encoding="utf-8").strip())
        assert row["threshold_pct"] == 0.13
        assert row["basis_price"] == 1e-6
        assert row["action_label"] == "价格硬止损(-13%)"
        assert "-13%" in row["exit_reason"]

    def test_old_record_keeps_its_own_threshold_after_retune(
        self, isolated_journal, monkeypatch
    ):
        """记录写完后把配置调走，重新读出来的标签必须还是原来那个阈值。"""
        journal.record_trade(
            action="hard_stop", mint="m1", symbol="OLD", amount_sol=0.02,
            price=1.0, pnl_sol=-0.006, pnl_percent=-13.0, threshold_pct=0.13,
        )
        monkeypatch.setattr(C, "HARD_STOP_PCT", 0.22)
        monkeypatch.setattr(C, "TRACK_A_HARD_STOP", 0.22)

        loaded = journal.load_trades(hours=24)[0]
        assert loaded["action_label"] == "价格硬止损(-13%)"
        assert "-13%" in loaded["exit_reason"]
        assert "-22%" not in loaded["exit_reason"]

    def test_legacy_record_without_threshold_is_not_relabelled(self, isolated_journal):
        """历史文件里没有 threshold_pct 的行：保留它自带的 action_label，不重渲染。"""
        journal.ensure_dirs()
        legacy = {
            "timestamp": journal._utc_iso(),
            "event": "hard_stop",
            "action": "hard_stop",
            "action_label": "价格硬止损(-13%)",
            "exit_reason": "价格硬止损（浮亏≤-13%，立刻全仓斩仓）",
            "mint": "m1",
            "symbol": "OLD",
            "amount_sol": 0.02,
            "price": 1.0,
            "pnl_sol": -0.006,
        }
        with C.DAILY_TRADES_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(legacy, ensure_ascii=False) + "\n")

        loaded = journal.load_trades(hours=24)[0]
        assert loaded["action_label"] == "价格硬止损(-13%)"
        assert loaded["exit_reason"] == legacy["exit_reason"]

    def test_legacy_record_with_no_label_gets_number_free_label(self, isolated_journal):
        journal.ensure_dirs()
        legacy = {
            "timestamp": journal._utc_iso(),
            "event": "close_partial",
            "reason": "hard_stop",
            "mint": "m1",
            "symbol": "OLD",
            "qty": 1.0,
            "price": 1.0,
            "pnl_sol": -0.006,
        }
        with C.DAILY_TRADES_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(legacy, ensure_ascii=False) + "\n")

        loaded = journal.load_trades(hours=24)[0]
        assert loaded["action_label"] == "价格硬止损"
        assert "未记录" in loaded["exit_reason"]


class TestFiredThreshold:
    """execution 交给日志的阈值必须是那个仓位真正在用的那一个。"""

    @staticmethod
    def _pos(track="A"):
        return {"track": track, "entry": 1.0}

    @pytest.mark.parametrize("track", ["A", "B"])
    def test_threshold_follows_the_position_track(self, track):
        xp = _exit_params(self._pos(track))
        assert _fired_threshold(self._pos(track), "hard_stop")[0] == xp["hard_stop"]
        assert _fired_threshold(self._pos(track), "tp1") == (xp["tp1"], xp["tp1_sell"])
        assert _fired_threshold(self._pos(track), "trail_stop")[0] == xp["trail"]

    def test_tp1_label_matches_track_not_global_config(self, isolated_journal):
        """真正触发 TP1 的是 TRACK_A_TP1，不是全局 C.TP1_PCT——标签要跟前者。"""
        pos = self._pos("A")
        threshold, sell = _fired_threshold(pos, "tp1")
        assert threshold == float(C.TRACK_A_TP1)
        row = journal.record_trade(
            action="tp1", mint="m", symbol="T", amount_sol=0.01, price=1.0,
            threshold_pct=threshold, sell_ratio=sell,
        )
        want = int(round(float(C.TRACK_A_TP1) * 100))
        assert f"+{want}%" in row["action_label"]

    def test_panic_stop_reports_its_own_threshold(self):
        """崩塌止损走 PANIC_STOP_PCT，比轨道硬止损更深，标签不能写成硬止损那个数。"""
        pos = self._pos("A")
        pos["stop_fired_threshold"] = float(C.PANIC_STOP_PCT)
        assert _fired_threshold(pos, "hard_stop")[0] == float(C.PANIC_STOP_PCT)

    def test_close_partial_freezes_the_threshold(self, isolated_journal, monkeypatch):
        """端到端：一笔纸面 hard_stop 落盘后带着当时的阈值。"""
        broker = PaperBroker()
        broker.dry_run = True
        pos = {
            "id": "p1", "mint": "m", "symbol": "T", "entry": 1.0, "entry_mark": 1.0,
            "qty": 1.0, "qty_left": 1.0, "sol_spent": 1.0, "opened_at": 0.0,
            "track": "A", "dry_run": True, "shadow": False, "mark": 0.7,
        }
        broker.positions["m"] = pos
        trade = broker._close_partial(pos, 1.0, 0.7, "hard_stop")
        assert trade["threshold_pct"] == float(C.TRACK_A_HARD_STOP)
        assert trade["basis_price"] == 1.0
        want = int(round(float(C.TRACK_A_HARD_STOP) * 100))
        assert trade["action_label"] == f"价格硬止损(-{want}%)"
