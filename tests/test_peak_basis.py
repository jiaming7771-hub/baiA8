"""peak 的口径：移动止盈线与 dead_stop 的 peak_pnl 都必须算在 mark 口径上。

事故背景：pos["peak"] 用成交价（entry/mid）播种，却在 manage() 里跟 mark 口径的
现价和 mark_basis() 比。成交价含买方滑点 + DEX 费，结构性高于成交后的池价——
实测 CXMT 成交均价 0.0002752526 比 Jupiter 报价 0.000264382 高 4.11%，比成交后
第一次池价 0.0002617375 高 5.16%。后果有两条，方向都是坏的：

  1) trail_line = peak×(1−trail) 整体上移 1/k 倍（k = entry_mark/entry < 1），
     回撤止盈按更浅的回撤就开火；
  2) peak_pnl = (peak−basis)/basis 在开仓瞬间就是 1/k−1 ≈ +2%~+6%，只要超过
     DEAD_CUT_MIN_PNL（默认 +3%），死盘早砍就永远进不了那个 if。

第 2 条是「闸门恒不成立」，比阈值偏一点严重得多，所以单独钉住。
"""

from __future__ import annotations

import time

import pytest

from pumpfun import config as C
from pumpfun.execution import PaperBroker, mark_basis

# 实测楔子：成交价比成交后池价高 5.16%（CXMT，2026-07-26T11:52:19Z）
WEDGE = 0.0516


def _pos(*, entry: float, entry_mark: float | None, opened_at: float | None = None) -> dict:
    """按 open_long 落盘的字段形状造一个仓位（peak 用修好后的口径播种）。"""
    pos = {
        "id": "p1",
        "mint": "m",
        "symbol": "T",
        "entry": entry,
        "entry_mark": entry_mark,
        "qty": 1.0,
        "qty_left": 1.0,
        "sol_spent": 1.0,
        "opened_at": opened_at if opened_at is not None else time.time(),
        "dry_run": True,
        "shadow": False,
        "track": "A",
        "tp1_done": False,
        "trail_line": None,
        "peak": entry_mark or entry,
        "peak_basis": "entry_mark" if entry_mark else "fill",
        "mark": entry_mark or entry,
        "max_float_pnl_pct": None,
        "min_float_pnl_pct": None,
    }
    return pos


class TestPeakSeed:
    def test_seeded_on_mark_basis_not_fill(self):
        """有 entry_mark 时 peak 必须等于它，不能等于成交价。"""
        entry = 1.0
        chain = entry * (1.0 - WEDGE)
        pos = _pos(entry=entry, entry_mark=chain)
        assert pos["peak"] == pytest.approx(chain)
        assert pos["peak"] == pytest.approx(mark_basis(pos))
        assert pos["peak_basis"] == "entry_mark"

    def test_falls_back_to_fill_when_no_entry_mark(self):
        """读不到链上基准时 peak 退回成交价——此时 mark_basis 也是成交价，仍然同源。"""
        pos = _pos(entry=1.0, entry_mark=None)
        assert pos["peak"] == pytest.approx(1.0)
        assert pos["peak"] == pytest.approx(mark_basis(pos))
        assert pos["peak_basis"] == "fill"

    def test_fresh_position_has_zero_peak_pnl(self):
        """开仓瞬间（现价 = 基准）峰值浮盈必须是 0，不是那个滑点楔子。"""
        entry = 1.0
        chain = entry * (1.0 - WEDGE)
        pos = _pos(entry=entry, entry_mark=chain)
        basis = mark_basis(pos)
        peak_pnl = (float(pos["peak"]) - basis) / basis
        assert peak_pnl == pytest.approx(0.0)
        # 旧口径下同一个仓位的峰值浮盈：楔子直接冒充浮盈
        stale_peak_pnl = (entry - basis) / basis
        assert stale_peak_pnl == pytest.approx(WEDGE / (1 - WEDGE), rel=1e-6)


class TestDeadStopReachable:
    """闸门可达性：楔子 > DEAD_CUT_MIN_PNL 时旧口径让 dead_stop 恒不触发。"""

    def test_old_seed_makes_dead_stop_unreachable_when_wedge_exceeds_threshold(self):
        entry = 1.0
        # 取一个刚好超过阈值的楔子，阈值调参后本测试依然有效
        wedge = float(C.DEAD_CUT_MIN_PNL) + 0.01
        chain = entry / (1.0 + wedge)
        basis = chain
        assert (entry - basis) / basis > float(C.DEAD_CUT_MIN_PNL)  # 旧口径：进不去
        pos = _pos(entry=entry, entry_mark=chain)
        assert (float(pos["peak"]) - basis) / basis < float(C.DEAD_CUT_MIN_PNL)

    def test_dead_stop_fires_on_flat_position_after_fix(self, monkeypatch):
        """楔子超阈值 + 价格原地不动 + 无量 → 死盘早砍必须开火。"""
        monkeypatch.setattr(C, "DEAD_CUT_ENABLED", True)
        monkeypatch.setattr(C, "IS_MOMENTUM", True)
        import pumpfun.market_data as md

        monkeypatch.setattr(md, "lookup_activity", lambda *a, **k: {"volume_m5_sol": 0.0})
        entry = 1.0
        chain = entry / (1.0 + float(C.DEAD_CUT_MIN_PNL) + 0.01)
        pos = _pos(
            entry=entry,
            entry_mark=chain,
            opened_at=time.time() - float(C.DEAD_CUT_SECONDS) - 5.0,
        )
        pos["volume_m5_sol"] = 100.0
        broker = PaperBroker()
        broker.dry_run = True
        broker.positions["m"] = pos
        events = broker.manage({"m": chain})
        assert [e["type"] for e in events] == ["dead_stop"]
        assert events[0]["peak_pnl"] == pytest.approx(0.0)

    def test_old_fill_basis_seed_would_never_fire(self, monkeypatch):
        """同一个僵尸仓位，peak 换回成交价口径就砍不动了——这才是闸门恒不成立。"""
        monkeypatch.setattr(C, "DEAD_CUT_ENABLED", True)
        monkeypatch.setattr(C, "IS_MOMENTUM", True)
        import pumpfun.market_data as md

        monkeypatch.setattr(md, "lookup_activity", lambda *a, **k: {"volume_m5_sol": 0.0})
        entry = 1.0
        chain = entry / (1.0 + float(C.DEAD_CUT_MIN_PNL) + 0.01)
        pos = _pos(
            entry=entry,
            entry_mark=chain,
            opened_at=time.time() - float(C.DEAD_CUT_SECONDS) - 5.0,
        )
        pos["volume_m5_sol"] = 100.0
        pos["peak"] = entry  # 旧口径
        pos["peak_basis"] = "fill"
        broker = PaperBroker()
        broker.dry_run = True
        broker.positions["m"] = pos
        assert broker.manage({"m": chain}) == []


class TestTrailLineBasis:
    def test_trail_line_sits_exactly_trail_below_mark_basis(self):
        """开仓即跟峰（TP1_SELL≤0）时，移动止盈线必须是 basis×(1−trail)。

        旧口径下它是 entry×(1−trail)，也就是高了 1/k 倍：k=0.9509（CXMT 实测）
        且 trail=20% 时，触发位从 −20.0% 变成 −15.9%，提前 4.1 个百分点开火。
        """
        entry = 1.0
        chain = entry * (1.0 - WEDGE)
        trail = float(C.TRACK_A_TRAIL)
        pos = _pos(entry=entry, entry_mark=chain)
        broker = PaperBroker()
        broker.dry_run = True
        broker.positions["m"] = pos
        pos["tp1_done"] = True
        broker.mark("m", chain)
        line = float(pos["trail_line"])
        assert line == pytest.approx(chain * (1.0 - trail))
        fired_at = line / chain - 1.0
        assert fired_at == pytest.approx(-trail)
        # 旧口径的触发位（更浅的回撤就开火）
        stale_fired_at = (entry * (1.0 - trail)) / chain - 1.0
        assert stale_fired_at > fired_at

    def test_peak_still_tracks_new_highs(self):
        """修基准不能顺手把跟峰关掉：新高必须抬 peak 和 trail_line。"""
        entry = 1.0
        chain = entry * (1.0 - WEDGE)
        trail = float(C.TRACK_A_TRAIL)
        pos = _pos(entry=entry, entry_mark=chain)
        broker = PaperBroker()
        broker.dry_run = True
        broker.positions["m"] = pos
        pos["tp1_done"] = True
        high = chain * 1.6
        broker.mark("m", high)
        assert float(pos["peak"]) == pytest.approx(high)
        assert float(pos["trail_line"]) == pytest.approx(high * (1.0 - trail))
        # 回落不得下调 peak
        broker.mark("m", chain)
        assert float(pos["peak"]) == pytest.approx(high)


def test_open_long_seeds_peak_from_entry_mark(monkeypatch):
    """走一遍 _open_long_body：落盘的 peak 必须是 entry_mark，不是成交价。

    这里刻意让「买前池价」和「成交后池价」不同：买前读到 entry（它会成为纸面
    成交价 mid），成交后读到 chain（低一个楔子）。旧代码 peak=mid，正好是要钉住
    的那个错；纸面模式下 mid 恰好等于买前池价，所以两个读数必须分开造，否则
    这条回归测试对旧代码也会通过。
    """
    broker = PaperBroker()
    broker.dry_run = True
    broker.shadow = False
    broker.cash = 10.0
    entry = 1e-6
    chain = entry * (1.0 - WEDGE)
    import pumpfun.onchain_price as op

    monkeypatch.setattr(
        op, "fetch_pool_price_sol", lambda *a, **k: {"price": entry, "source": "test"}
    )
    monkeypatch.setattr(C, "ENTRY_CONFIRM_SEC", 0.0)
    monkeypatch.setattr(C, "SAFETY_CHECK_ENABLED", False)
    monkeypatch.setattr(C, "BONDING_MIN_PROGRESS_PCT", 0.0)
    monkeypatch.setattr(C, "CREATOR_BAN_ENABLED", False)
    monkeypatch.setattr(broker, "_read_entry_mark", lambda *a, **k: chain)
    signal = {
        "mint": "MintPeakBasis",
        "symbol": "PKB",
        "price": entry,
        "pool": "pool1",
        "dex": "pumpswap",
        "track": "A",
        "score": float(C.ENTRY_MIN_SCORE) + 10.0,
    }
    pos = broker.open_long(signal, dry_run=True)
    assert pos is not None
    assert pos["entry"] == pytest.approx(entry)
    assert pos["peak"] == pytest.approx(chain)
    assert pos["peak"] != pytest.approx(pos["entry"])
    assert pos["peak_basis"] == "entry_mark"
    assert pos["peak"] == pytest.approx(mark_basis(pos))
