"""持仓「标价冻结」逃生：读不到链上价时不得静默沿用旧 mark。

事故背景（NOTCOON，mint B6xYWb1f…，2026-07-26）：池子 owner 是 Meteora DBC
程序，链上读价器不认识 → 每轮返回 None → fetch_prices_for_positions 不产出
该 mint → manage() 退回 `pos["mark"]` 继续判定。止损/止盈整整 12 分钟都在拿
一个不动的数跟自己比，等于没有风控；最终 tp1 在 -97.7% 触发、-99.6% 核销。

这里锁死两件事：
1) 读不到价必须留下可见状态（mark_stale_since / mark_stale_reason），不是一行日志；
2) 超过 C.MARK_STALE_MAX_SEC 必须强制离场，而不是继续等。
"""

from __future__ import annotations

import time

import pytest

from pumpfun import config as C
from pumpfun import execution as EX
from pumpfun import onchain_price as op
from pumpfun.execution import PaperBroker


def _pos(**over):
    pos = {
        "id": "p1",
        "mint": "m",
        "symbol": "NOTCOON",
        "entry": 1e-6,
        "entry_mark": 1e-6,
        "mark": 1e-6,
        "peak": 1e-6,
        "qty": 1000.0,
        "qty_left": 1000.0,
        "qty_raw": 1_000_000_000,
        "decimals": 6,
        "sol_spent": 0.05,
        "opened_at": time.time() - 300,
        "track": "A",
        "dry_run": False,
        "shadow": False,
        "dex": "meteoradbc",
    }
    pos.update(over)
    return pos


class TestStaleMarkTracking:
    """fetch_prices_for_positions 必须把「读不到」变成一个上层看得见的状态。"""

    def test_failure_starts_the_clock_and_records_reason(self, monkeypatch):
        monkeypatch.setattr(
            op, "fetch_pool_price_row", lambda *a, **k: (None, "unknown_owner:dbcij3LW")
        )
        positions = {"m": _pos()}
        assert op.fetch_prices_for_positions(positions) == {}
        assert positions["m"]["mark_stale_since"] > 0
        assert positions["m"]["mark_stale_reason"] == "unknown_owner:dbcij3LW"

    def test_clock_does_not_restart_on_repeated_failures(self, monkeypatch):
        monkeypatch.setattr(op, "fetch_pool_price_row", lambda *a, **k: (None, "rpc:boom"))
        positions = {"m": _pos()}
        op.fetch_prices_for_positions(positions)
        first = positions["m"]["mark_stale_since"]
        time.sleep(0.01)
        op.fetch_prices_for_positions(positions)
        assert positions["m"]["mark_stale_since"] == first

    def test_success_clears_the_clock(self, monkeypatch):
        positions = {"m": _pos(mark_stale_since=time.time() - 60, mark_stale_reason="x")}
        monkeypatch.setattr(
            op,
            "fetch_pool_price_row",
            lambda *a, **k: (
                {"price": 2e-6, "source": "meteora_dbc", "ts": time.time(), "pool": "P"},
                "",
            ),
        )
        assert op.fetch_prices_for_positions(positions)["m"] == 2e-6
        assert "mark_stale_since" not in positions["m"]
        assert "mark_stale_reason" not in positions["m"]

    def test_drained_sentinel_is_not_treated_as_stale(self, monkeypatch):
        """抽干哨兵是一个**真实**读数：它要走抽池逃生，不能被算成读不到。"""
        monkeypatch.setattr(
            op,
            "fetch_pool_price_row",
            lambda *a, **k: (
                {
                    "price": 1e-18,
                    "source": "meteora_dbc_drained",
                    "ts": time.time(),
                    "sol_vault": 0.0,
                    "vault_drained": True,
                },
                "",
            ),
        )
        positions = {"m": _pos(entry_sol_vault=10.0)}
        assert op.fetch_prices_for_positions(positions)["m"] == 1e-18
        assert "mark_stale_since" not in positions["m"]
        assert positions["m"]["vault_drain"] is True


class TestStaleMarkEscape:
    """manage() 必须在超时后强制离场。"""

    @staticmethod
    def _broker(monkeypatch, pos):
        broker = PaperBroker()
        broker.dry_run = False
        broker.positions["m"] = pos
        sold: list[dict] = []

        def fake_close(p, ratio, price, reason):
            sold.append({"ratio": ratio, "price": price, "reason": reason})
            p["qty_left"] = 0.0
            return {"action": reason, "pnl_sol": -0.01}

        monkeypatch.setattr(broker, "_close_partial", fake_close)
        return broker, sold

    def test_escape_fires_after_configured_limit(self, monkeypatch):
        limit = float(C.MARK_STALE_MAX_SEC)
        pos = _pos(mark_stale_since=time.time() - limit - 1, mark_stale_reason="dbc_migrated")
        broker, sold = self._broker(monkeypatch, pos)

        events = broker.manage({})  # 一个价都没报上来，正是最危险的那种轮次

        assert [e["type"] for e in events] == ["stale_mark_escape"]
        assert sold and sold[0]["reason"] == "stale_mark"
        assert sold[0]["ratio"] == 1.0
        assert events[0]["stale_reason"] == "dbc_migrated"
        assert events[0]["stale_sec"] >= limit
        assert "m" not in broker.positions

    def test_no_escape_before_the_limit(self, monkeypatch):
        limit = float(C.MARK_STALE_MAX_SEC)
        pos = _pos(mark_stale_since=time.time() - limit * 0.5)
        broker, sold = self._broker(monkeypatch, pos)

        assert broker.manage({}) == []
        assert sold == []
        assert "m" in broker.positions

    def test_fresh_price_never_escapes(self, monkeypatch):
        pos = _pos()
        broker, sold = self._broker(monkeypatch, pos)
        assert broker.manage({"m": 1e-6}) == []
        assert sold == []

    def test_paper_and_shadow_positions_are_left_alone(self, monkeypatch):
        limit = float(C.MARK_STALE_MAX_SEC)
        for flag in ("dry_run", "shadow"):
            pos = _pos(mark_stale_since=time.time() - limit - 1, **{flag: True})
            broker, sold = self._broker(monkeypatch, pos)
            assert broker.manage({}) == [], flag
            assert sold == [], flag

    def test_disabled_by_zero_config(self, monkeypatch):
        monkeypatch.setattr(C, "MARK_STALE_MAX_SEC", 0.0)
        pos = _pos(mark_stale_since=time.time() - 100_000)
        broker, sold = self._broker(monkeypatch, pos)
        assert broker.manage({}) == []
        assert sold == []

    def test_stale_position_would_otherwise_ride_the_frozen_mark(self, monkeypatch):
        """对照组：关掉逃生后，读不到价的仓位确实在拿旧 mark 空转（根因回归）。

        真实价格已经腰斩到硬止损线以下，但因为 price_map 里没有它，manage()
        退回 pos["mark"]，一轮都不会触发止损。
        """
        monkeypatch.setattr(C, "MARK_STALE_MAX_SEC", 0.0)
        stop = float(C.TRACK_A_HARD_STOP)
        pos = _pos(mark_stale_since=time.time() - 100_000)
        broker, sold = self._broker(monkeypatch, pos)

        for _ in range(5):
            assert broker.manage({}) == []
            if pos.get("stop_arm_ts"):
                pos["stop_arm_ts"] -= float(C.HARD_STOP_CONFIRM_SEC) + 1.0
        assert pos["mark"] == 1e-6
        assert pos["pnl_pct"] == pytest.approx(0.0)

        # 同一个仓位，同一个价格，只要报上来就会被砍——差别只在"读不读得到"
        crashed = 1e-6 * (1.0 - stop - 0.05)
        for _ in range(5):
            broker.manage({"m": crashed})
            if pos.get("stop_arm_ts"):
                pos["stop_arm_ts"] -= float(C.HARD_STOP_CONFIRM_SEC) + 1.0
        assert sold, "价格报上来后必须有出场动作"
        assert pos["pnl_pct"] == pytest.approx(-stop - 0.05, rel=1e-6)


def test_stale_mark_is_an_urgent_sell_reason():
    """逃生单必须走保命单通道（允许滑点升级重试），否则卡在 Mempool 等于没砍。"""
    src = EX.__file__
    with open(src, encoding="utf-8") as f:
        text = f.read()
    urgent_block = text.split("urgent = reason in (", 1)[1].split(")", 1)[0]
    assert '"stale_mark"' in urgent_block


def test_snapshot_exposes_stale_age():
    broker = PaperBroker()
    broker.positions["m"] = _pos(mark_stale_since=time.time() - 30, mark_stale_reason="rpc:x")
    row = broker.snapshot_positions()[0]
    assert row["mark_stale_sec"] >= 29
    assert row["mark_stale_reason"] == "rpc:x"
