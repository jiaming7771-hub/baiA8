"""浮盈亏极值必须是**真实出现过的读数**，不是被 0 卡住的下限。

事故背景：max_float_pnl_pct 在开仓时初始化成 0.0，之后只允许上调。于是全程
水下的仓位永远记成「峰值 +0.00%」——一个从没出现过的数，却和真实测量值长得
一模一样。27 笔历史仓位里 20 笔是 0.00，其中包含 -18% 硬止损的那几笔：拿这份
数据做「分数 vs 结果」的校准，等于拿一列常数去拟合。

这不是漏写，是定义错误。所以修的是定义（真实运行极值 + 同时记最低点），
不是补一次写入。
"""

from __future__ import annotations

import json

import pytest

from pumpfun import config as C
from pumpfun import journal
from pumpfun.execution import PaperBroker, mark_basis


def _pos(pid="p1", *, entry=1.0, entry_mark=None, qty=1.0):
    pos = {
        "id": pid, "mint": "m", "symbol": "T", "entry": entry,
        "qty": qty, "qty_left": qty, "sol_spent": 1.0, "opened_at": 0.0,
        "track": "A", "dry_run": True, "shadow": False, "tp1_done": False,
        "max_float_pnl_pct": None, "max_float_pnl_sol": None,
        "min_float_pnl_pct": None, "min_float_pnl_sol": None,
    }
    if entry_mark is not None:
        pos["entry_mark"] = entry_mark
    base = mark_basis(pos)
    pos["peak"] = base
    pos["mark"] = base
    return pos


def _broker(pos):
    b = PaperBroker()
    b.dry_run = True
    b.positions["m"] = pos
    return b


class TestExtremaAreRealReadings:
    def test_never_marked_is_none_not_zero(self):
        """没标过价 = 没测到。0.0 会被当成「峰值恰好 0.00%」的测量值。"""
        pos = _pos()
        _broker(pos)
        assert pos["max_float_pnl_pct"] is None
        assert pos["min_float_pnl_pct"] is None

    def test_underwater_position_records_negative_peak(self):
        """全程水下：峰值必须是 -5%（真实最高点），不是 0。"""
        pos = _pos(entry_mark=1.0)
        b = _broker(pos)
        for px in (0.95, 0.90, 0.82, 0.88):
            b.mark("m", px)
        assert pos["max_float_pnl_pct"] == pytest.approx(-5.0)
        assert pos["min_float_pnl_pct"] == pytest.approx(-18.0)

    def test_old_definition_would_have_reported_zero(self):
        """复现历史病征：同一串价格在「初始 0 且只上调」下记成 0.00。

        这条断言锁住的是「为什么历史 27 笔不可用」，不是新行为。
        """
        legacy = 0.0
        for px in (0.95, 0.90, 0.82, 0.88):
            legacy = max(legacy, (px - 1.0) / 1.0 * 100.0)
        assert legacy == 0.0

        pos = _pos(entry_mark=1.0)
        b = _broker(pos)
        for px in (0.95, 0.90, 0.82, 0.88):
            b.mark("m", px)
        assert pos["max_float_pnl_pct"] < 0

    def test_drawdown_before_recovery_is_visible(self):
        """先跌 30% 再涨到 +10%：只记最高点看不出这笔曾经深水，最低点必须在。"""
        pos = _pos(entry_mark=1.0)
        b = _broker(pos)
        for px in (0.70, 0.85, 1.10):
            b.mark("m", px)
        assert pos["max_float_pnl_pct"] == pytest.approx(10.0)
        assert pos["min_float_pnl_pct"] == pytest.approx(-30.0)

    def test_extrema_never_regress(self):
        """极值是单调的：后续行情不得把已经出现过的极值改回去。"""
        pos = _pos(entry_mark=1.0)
        b = _broker(pos)
        b.mark("m", 1.40)
        b.mark("m", 0.60)
        b.mark("m", 1.00)
        assert pos["max_float_pnl_pct"] == pytest.approx(40.0)
        assert pos["min_float_pnl_pct"] == pytest.approx(-40.0)


class TestExtremaBasis:
    """极值必须算在 mark_basis 上，且记录要说明自己算在哪个基准上。"""

    def test_uses_entry_mark_not_fill_price(self):
        """成交价 1.0、链上基准 0.8（低报 1.25 倍）：链上价平走 = 0%，不是 -20%。"""
        pos = _pos(entry=1.0, entry_mark=0.8)
        b = _broker(pos)
        b.mark("m", 0.8)
        assert pos["max_float_pnl_pct"] == pytest.approx(0.0)
        assert pos["min_float_pnl_pct"] == pytest.approx(0.0)
        # 成交价口径同时保留，但不得污染极值
        assert pos["pnl_pct_vs_fill"] == pytest.approx(-0.2)

    def test_falls_back_to_fill_basis_when_no_entry_mark(self):
        pos = _pos(entry=1.0)
        b = _broker(pos)
        b.mark("m", 0.9)
        assert mark_basis(pos) == 1.0
        assert pos["max_float_pnl_pct"] == pytest.approx(-10.0)


class TestJournalCarriesExtrema:
    def test_exit_record_carries_extrema_and_basis(self):
        pos = _pos(entry=1.0, entry_mark=0.8)
        b = _broker(pos)
        for px in (0.76, 0.72, 0.78):
            b.mark("m", px)
        trade = b._close_partial(pos, 1.0, 0.78, "hard_stop")

        assert trade["max_float_pnl_pct"] == pytest.approx(-2.5)
        assert trade["min_float_pnl_pct"] == pytest.approx(-10.0)
        # 极值算在 entry_mark 上；同记录里的 pnl_percent 是成交价口径，
        # 没有这个标记两个数会被当成同一把尺子
        assert trade["float_basis"] == "entry_mark"
        assert trade["basis_price"] == pytest.approx(0.8)
        assert trade["pnl_percent"] == pytest.approx(-22.0)

    def test_fill_basis_is_labelled_as_such(self):
        pos = _pos(entry=1.0)
        b = _broker(pos)
        b.mark("m", 0.9)
        trade = b._close_partial(pos, 1.0, 0.9, "hard_stop")
        assert trade["float_basis"] == "fill"

    def test_extrema_survive_the_jsonl_roundtrip(self):
        pos = _pos(entry=1.0, entry_mark=1.0)
        b = _broker(pos)
        for px in (0.9, 0.85):
            b.mark("m", px)
        b._close_partial(pos, 1.0, 0.85, "hard_stop")

        rows = [
            json.loads(line)
            for line in C.DAILY_TRADES_FILE.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        exits = [r for r in rows if r["action"] == "hard_stop"]
        assert exits, "出场记录没落盘"
        assert exits[-1]["max_float_pnl_pct"] == pytest.approx(-10.0)
        assert exits[-1]["min_float_pnl_pct"] == pytest.approx(-15.0)
        assert exits[-1]["float_basis"] == "entry_mark"


class TestRealHardStopKeepsItsPeak:
    """端到端：真跌穿配置的硬止损线后，那笔记录里的峰值必须是真实峰值。"""

    def test_hard_stop_records_true_peak_not_zero(self):
        stop = max(float(C.TRACK_A_HARD_STOP), float(C.HARD_STOP_PCT))
        panic = max(stop, float(C.PANIC_STOP_PCT))
        basis = 1.0
        # 先小幅浮亏（峰值仍为负），再一次性砸穿崩塌线以免等确认窗口
        peak_px = basis * (1.0 - stop / 4.0)
        kill_px = basis * (1.0 - panic - 0.05)

        pos = _pos(entry=basis, entry_mark=basis)
        b = _broker(pos)
        b.manage({"m": peak_px})
        events = b.manage({"m": kill_px})

        assert any(e["type"] == "hard_stop" for e in events), events
        want_peak = (peak_px - basis) / basis * 100.0
        assert pos["max_float_pnl_pct"] == pytest.approx(want_peak, abs=1e-4)
        assert pos["max_float_pnl_pct"] < 0
        trade = [e for e in events if e["type"] == "hard_stop"][0]["trade"]
        assert trade["max_float_pnl_pct"] == pytest.approx(want_peak, abs=1e-4)


class TestEarlyFadeStillReadsPeak:
    """early_fade 用 max_float_pnl_pct 判「从未真正浮盈」；改成可为负后仍需成立。"""

    def test_negative_peak_counts_as_never_profitable(self):
        pos = _pos(entry=1.0, entry_mark=1.0)
        b = _broker(pos)
        b.mark("m", 0.97)
        peak_ratio = float(pos["max_float_pnl_pct"]) / 100.0
        assert peak_ratio < 0
        assert peak_ratio <= float(C.EARLY_FADE_MAX_PEAK)


def test_buy_record_has_no_fabricated_peak():
    """买入当下还没标过价，记录里不能出现 0.00% 的假峰值。"""
    journal.record_trade(
        action="buy", mint="m", symbol="T", amount_sol=0.05, price=1.0,
        metrics={"max_float_pnl_pct": None, "min_float_pnl_pct": None,
                 "float_basis": "fill"},
    )
    row = json.loads(
        C.DAILY_TRADES_FILE.read_text(encoding="utf-8").splitlines()[0]
    )
    assert row["max_float_pnl_pct"] is None
    assert row["min_float_pnl_pct"] is None
