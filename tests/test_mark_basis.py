"""标价口径一致性：出场阶梯必须 mark 对 mark 算，不吃成交价↔链上价的基差。

事故背景：entry 记 Jupiter 真实成交价，之后每轮用链上池价标价。链上池价曾漏算
PumpSwap 虚拟储备而系统性低报 1.05~1.5 倍 → 开仓瞬间就是 −5%~−33% 假浮亏，
硬止损 −13% 实际变成「真实盈亏跌破 −3%~+9% 就砍」，TP1 永远够不着。
"""

from __future__ import annotations

import pytest

from pumpfun import config as C
from pumpfun import execution as EX
from pumpfun.execution import PaperBroker, mark_basis


class TestMarkBasis:
    def test_falls_back_to_entry_when_no_entry_mark(self):
        assert mark_basis({"entry": 2.0}) == 2.0
        assert mark_basis({"entry": 2.0, "entry_mark": None}) == 2.0
        assert mark_basis({"entry": 2.0, "entry_mark": 0}) == 2.0

    def test_prefers_entry_mark(self):
        assert mark_basis({"entry": 2.0, "entry_mark": 1.6}) == 1.6

    def test_pnl_uses_mark_basis_not_fill(self):
        """成交价 1.0、链上基准 0.8（低报 1.25 倍）时，链上现价 0.8 应是 0%。"""
        pos = {"entry": 1.0, "entry_mark": 0.8, "peak": 0.0}
        broker = PaperBroker()
        broker.dry_run = True
        broker.positions["m"] = pos
        broker.mark("m", 0.8)
        assert pos["pnl_pct"] == pytest.approx(0.0)
        # 账本口径同时保留，供审计/看板（−20%）
        assert pos["pnl_pct_vs_fill"] == pytest.approx(-0.2)

    @staticmethod
    def _pos(pid, entry, *, entry_mark=None, peak=None, opened_at=0.0):
        pos = {
            "id": pid, "mint": "m", "symbol": "T", "entry": entry,
            "qty": 1.0, "qty_left": 1.0, "opened_at": opened_at,
            "dry_run": True, "shadow": False, "track": "A",
            "tp1_done": False, "sol_spent": 1.0,
        }
        if entry_mark is not None:
            pos["entry_mark"] = entry_mark
        base = entry_mark if entry_mark is not None else entry
        pos["peak"] = peak if peak is not None else base
        pos["mark"] = base
        return pos

    def _drive(self, pos, px, rounds=4):
        """跑够轮数满足 HARD_STOP_CONFIRM_TICKS/SEC 的连续确认。"""
        broker = PaperBroker()
        broker.dry_run = True
        broker.positions["m"] = pos
        seen = []
        for _ in range(rounds):
            seen += [e["type"] for e in broker.manage({"m": px})]
            if "m" not in broker.positions:
                break
            # 把警戒起点往过去推，绕过 CONFIRM_SEC 的墙钟等待
            if pos.get("stop_arm_ts"):
                pos["stop_arm_ts"] -= float(C.HARD_STOP_CONFIRM_SEC) + 1.0
        return seen

    def test_hard_stop_not_fired_by_pure_basis_gap(self):
        """纯基差（无真实下跌）不得触发硬止损。

        低报倍数取「刚好能踩穿当前硬止损线」的值，这样阈值调参后本测试依然有效。
        """
        entry_fill = 1.0
        stop = max(float(C.TRACK_A_HARD_STOP), float(C.HARD_STOP_PCT))
        factor = 1.0 / (1.0 - stop - 0.05)   # 例：stop=22% → 1.37 倍低报
        chain = entry_fill / factor          # 账本口径 −27%，真实 0%
        got = self._drive(self._pos("t1", entry_fill, entry_mark=chain), chain)
        assert got == []

        # 对照组：不带 entry_mark（旧行为）→ 同样的输入会被误杀
        got_old = self._drive(self._pos("t2", entry_fill), chain)
        assert "hard_stop" in got_old

    def test_real_drawdown_still_stops(self):
        """基准正确后，真实跌破硬止损线仍必须砍（别把闸门关掉）。"""
        chain = 0.8
        drop = max(float(C.TRACK_A_HARD_STOP), float(C.HARD_STOP_PCT)) + 0.05
        got = self._drive(
            self._pos("t3", 1.0, entry_mark=chain), chain * (1.0 - drop)
        )
        assert "hard_stop" in got


class TestReadEntryMark:
    def _broker(self):
        b = PaperBroker()
        b.dry_run = True
        return b

    def test_rejects_absurd_reading(self, monkeypatch):
        """链上读数偏离成交价一倍以上 → 不采信，退回成交价基准。"""
        import pumpfun.onchain_price as op

        monkeypatch.setattr(
            op, "fetch_pool_price_sol", lambda *a, **k: {"price": 1e-9}
        )
        got = self._broker()._read_entry_mark(
            "m", {"symbol": "T"}, fill_ref=1e-6
        )
        assert got == 0.0

    def test_accepts_plausible_reading(self, monkeypatch):
        import pumpfun.onchain_price as op

        monkeypatch.setattr(
            op, "fetch_pool_price_sol", lambda *a, **k: {"price": 9.5e-7}
        )
        got = self._broker()._read_entry_mark("m", {"symbol": "T"}, fill_ref=1e-6)
        assert got == 9.5e-7

    def test_rejects_drained_pool_reading(self, monkeypatch):
        import pumpfun.onchain_price as op

        monkeypatch.setattr(
            op,
            "fetch_pool_price_sol",
            lambda *a, **k: {"price": 1e-6, "vault_drained": True},
        )
        assert self._broker()._read_entry_mark(
            "m", {"symbol": "T"}, fill_ref=1e-6
        ) == 0.0

    def test_rpc_failure_falls_back(self, monkeypatch):
        import pumpfun.onchain_price as op

        def boom(*a, **k):
            raise RuntimeError("rpc down")

        monkeypatch.setattr(op, "fetch_pool_price_sol", boom)
        assert self._broker()._read_entry_mark(
            "m", {"symbol": "T"}, fill_ref=1e-6
        ) == 0.0

    def test_zero_fill_ref_is_safe(self):
        assert self._broker()._read_entry_mark("m", {}, fill_ref=0.0) == 0.0


def test_config_gap_bounds():
    assert 1.05 <= float(C.ENTRY_MARK_MAX_GAP) <= 10.0


def test_mark_basis_exported():
    assert callable(EX.mark_basis)
