"""买入记录必须自带打分口径，否则跨版本的分数没法比。

事故背景：activity_s 从饱和线性改成对数刻度（e7d36a7）之后，分数的量纲就变了。
历史 journal 里的分数横跨 39.9~75.7，而新口径的可达上限只有 56.77——两批数字
混在同一列 `score` 里，没有任何字段能把它们分开。于是「多少分以上值得买」这个
问题在整份历史上都无解：任何阈值都是在两把不同的尺子上平均出来的。

修法沿用 1d7afea 给出场记录存 threshold_pct / sell_ratio / basis_price 的做法：
把**当时**的口径（版本+分项+权重+折扣）和**当时**的准入线跟分数存在一起。
"""

from __future__ import annotations

import json

import pytest

from pumpfun import config as C
from pumpfun import journal
from pumpfun import strategy as S
from pumpfun.execution import PaperBroker


def _candidate():
    rows = S.generate_demo_universe()
    assert rows, "demo universe 为空，无法构造候选"
    return rows[0]


class TestBreakdownReconstructsScore:
    def test_parts_times_weights_equals_score(self):
        """记录自身可验算：Σ 分项×权重×折扣 必须还原出 score。

        这正是历史数据做不到的事——只存一个标量，事后无从判断它出自哪把尺子。
        """
        for c in S.generate_demo_universe():
            bd = S.score_breakdown(c)
            recon = round(
                sum(bd["weights"][k] * bd["parts"][k] for k in bd["weights"])
                * bd["mult"],
                2,
            )
            assert recon == pytest.approx(bd["score"], abs=0.02), bd

    def test_breakdown_agrees_with_score_candidate(self):
        for c in S.generate_demo_universe():
            assert S.score_breakdown(c)["score"] == S.score_candidate(c)

    def test_weights_cover_exactly_the_parts(self):
        bd = S.score_breakdown(_candidate())
        assert set(bd["weights"]) == set(bd["parts"])

    def test_version_is_present_and_positive(self):
        bd = S.score_breakdown(_candidate())
        assert bd["ver"] == S.SCORING_VERSION
        assert isinstance(bd["ver"], int) and bd["ver"] > 0

    def test_missing_ohlcv_discount_is_recorded_not_hidden(self):
        """无真实序列时的 0.8 折扣是量纲的一部分，必须出现在记录里。"""
        cands = S.generate_demo_universe()
        c = cands[0]
        c.ohlcv_ok = True
        assert S.score_breakdown(c)["mult"] == pytest.approx(1.0)
        c.ohlcv_ok = False
        # demo 自带可用自采序列 → 不打折
        assert c.self_hist_usable
        assert S.score_breakdown(c)["mult"] == pytest.approx(1.0)
        # 两边都没有 → 才记 ×0.8
        c.self_points = 0
        assert not c.self_hist_usable
        bd = S.score_breakdown(c)
        assert bd["mult"] == pytest.approx(S.NO_OHLCV_MULT)


class TestFilterCandidatesAttachesScoring:
    def test_row_carries_breakdown_matching_its_score(self, pin_filter_defaults):
        rows = S.filter_candidates(S.generate_demo_universe())
        assert rows
        for row in rows:
            bd = row["scoring"]
            assert bd["ver"] == S.SCORING_VERSION
            assert bd["score"] == row["score"]
            recon = round(
                sum(bd["weights"][k] * bd["parts"][k] for k in bd["weights"])
                * bd["mult"],
                2,
            )
            assert recon == pytest.approx(row["score"], abs=0.05), row["scoring"]


class TestBuyRecordIsSelfDescribing:
    @pytest.fixture()
    def broker(self, monkeypatch):
        # 曲线进度闸门要打 RPC，这里只关心记账；用配置项关掉而不是绕过代码
        monkeypatch.setattr(C, "BONDING_MIN_PROGRESS_PCT", 0.0)
        # 本机 .env 可能把仓位上限钉成 1，双买入用例需要至少 2
        monkeypatch.setattr(C, "MAX_OPEN_POSITIONS", 3)
        b = PaperBroker()
        b.dry_run = True
        return b

    @staticmethod
    def _signal(score):
        return {
            "mint": "MINTSCORE", "symbol": "SCOR", "price": 1e-6,
            "pool": "POOL", "dex": "pumpswap", "score": score,
            "track": "A", "scoring": S.score_breakdown(_candidate()),
        }

    def _buy_row(self):
        rows = [
            json.loads(line)
            for line in C.DAILY_TRADES_FILE.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        buys = [r for r in rows if r["action"] == "buy"]
        assert buys, "买入记录没落盘"
        return buys[-1]

    def test_buy_persists_scoring_block(self, broker):
        score = float(C.ENTRY_MIN_SCORE) + 5.0
        assert broker.open_long(self._signal(score)) is not None
        row = self._buy_row()
        assert row["score"] == score
        assert row["scoring"]["ver"] == S.SCORING_VERSION
        assert set(row["scoring"]["parts"]) == set(row["scoring"]["weights"])
        assert row["scoring"]["mode"] in ("momentum", "dip")

    def test_buy_persists_the_thresholds_actually_in_force(self, broker):
        assert broker.open_long(self._signal(float(C.ENTRY_MIN_SCORE) + 5.0))
        gate = self._buy_row()["entry_gate"]
        assert gate["min_score"] == float(C.ENTRY_MIN_SCORE)
        assert gate["graduated_only"] == bool(C.ENTRY_GRADUATED_ONLY)
        assert gate["bonding_min_pct"] == float(C.BONDING_MIN_PROGRESS_PCT)
        assert gate["ath_drop_max"] == float(C.ENTRY_ATH_DROP_MAX)
        assert gate["ath_drop_min"] == float(C.ENTRY_ATH_DROP_MIN)
        assert gate["track"] == "A"

    def test_recorded_gate_is_frozen_against_later_retune(self, broker, monkeypatch):
        """记录写完后改门槛，读出来的仍须是当时那条线（与 threshold_pct 同款保证）。"""
        was = float(C.ENTRY_MIN_SCORE)
        assert broker.open_long(self._signal(was + 5.0))
        monkeypatch.setattr(C, "ENTRY_MIN_SCORE", was + 20.0)
        loaded = [r for r in journal.load_trades(hours=24) if r["action"] == "buy"][0]
        assert loaded["entry_gate"]["min_score"] == was

    def test_regime_grouping_is_possible(self, broker, monkeypatch):
        """两个口径版本各写一笔，读回来必须能按版本分组——这正是历史做不到的。"""
        assert broker.open_long(self._signal(float(C.ENTRY_MIN_SCORE) + 5.0))
        monkeypatch.setattr(S, "SCORING_VERSION", S.SCORING_VERSION + 1)
        sig = self._signal(float(C.ENTRY_MIN_SCORE) + 6.0)
        sig["mint"] = "MINTSCORE2"
        sig["symbol"] = "SCR2"
        sig["scoring"] = S.score_breakdown(_candidate())
        assert broker.open_long(sig)

        buys = [r for r in journal.load_trades(hours=24) if r["action"] == "buy"]
        vers = {r["scoring"]["ver"] for r in buys}
        assert len(vers) == 2, buys


class TestOldRecordsStillLoad:
    def test_record_without_new_fields_loads_and_renders(self):
        """历史行没有 scoring / entry_gate，读取与渲染都不能因此炸掉。"""
        journal.ensure_dirs()
        legacy = {
            "timestamp": journal._utc_iso(),
            "action": "buy",
            "mint": "m1",
            "symbol": "OLD",
            "amount_sol": 0.05,
            "price": 1e-6,
            "score": 71.4,  # 旧口径的分，新口径根本到不了
        }
        with C.DAILY_TRADES_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(legacy, ensure_ascii=False) + "\n")

        loaded = journal.load_trades(hours=24)[0]
        assert loaded["score"] == 71.4
        assert loaded.get("scoring") is None
        assert loaded.get("entry_gate") is None
        assert loaded["action_label"] == "买入"

    def test_csv_export_tolerates_both_shapes(self):
        journal.record_trade(
            action="buy", mint="m", symbol="NEW", amount_sol=0.05, price=1e-6,
            scoring=S.score_breakdown(_candidate()),
            entry_gate={"min_score": float(C.ENTRY_MIN_SCORE)},
        )
        journal.record_trade(
            action="buy", mint="m2", symbol="OLDSHAPE", amount_sol=0.05, price=1e-6,
        )
        csv_text = journal.trades_to_csv(hours=24)
        assert "NEW" in csv_text and "OLDSHAPE" in csv_text

    def test_exit_records_do_not_carry_a_scoring_block(self):
        """打分是开仓侧的事；出场记录带一份只会是过期副本。"""
        row = journal.record_trade(
            action="hard_stop", mint="m", symbol="T", amount_sol=0.02,
            price=1.0, pnl_sol=-0.01, pnl_percent=-13.0, threshold_pct=0.13,
        )
        assert row["scoring"] is None
        assert row["entry_gate"] is None
