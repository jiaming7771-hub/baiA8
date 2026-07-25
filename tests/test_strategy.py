"""黄金猎杀过滤边界：ATH 区间 / m15 恐慌 / m5 活跃度 / 上线时长。"""

from __future__ import annotations

import time

import pytest

from pumpfun import config as C
from pumpfun.strategy import Candidate, pass_hard_filters


def _base_pass(**overrides) -> Candidate:
    """构造默认可通过严苛过滤的候选。"""
    now = time.time()
    ath = 1e-4
    price = ath * 0.40  # 跌幅 60%
    kwargs = dict(
        mint="mint_test_pass_000000000000000pump",
        symbol="PASSCOIN",
        listed_at=now - 60 * 60,  # 60 分钟
        ath_price=ath,
        price=price,
        buy_vol=10.0,
        sell_vol=15.0,  # m15 panic=1.5
        whale_dump_pct=0.50,
        liquidity_sol=20.0,
        tx_count_m5=10,
        volume_m5_sol=2.0,
    )
    kwargs.update(overrides)
    return Candidate(**kwargs)


class TestStrategyFilters:
    def test_ath_drop_399_blocked(self):
        ath = 1.0
        price = ath * (1.0 - 0.399)
        c = _base_pass(ath_price=ath, price=price)
        ok, fails = pass_hard_filters(c)
        assert not ok
        assert any("ATH" in f or "跌幅" in f for f in fails)

    def test_ath_drop_801_blocked_as_dead(self):
        ath = 1.0
        price = ath * (1.0 - 0.801)
        c = _base_pass(ath_price=ath, price=price)
        ok, fails = pass_hard_filters(c)
        assert not ok
        assert any("死币" in f for f in fails)

    def test_panic_119_blocked(self):
        c = _base_pass(buy_vol=100.0, sell_vol=119.0)
        assert c.panic_ratio == pytest.approx(1.19)
        ok, fails = pass_hard_filters(c)
        assert not ok
        assert any("恐慌" in f for f in fails)

    def test_panic_12_pass(self):
        c = _base_pass(buy_vol=10.0, sell_vol=12.0)
        assert c.panic_ratio == pytest.approx(1.2)
        ok, fails = pass_hard_filters(c)
        assert ok, fails

    def test_age_before_5m_blocked(self):
        c = _base_pass(listed_at=time.time() - 4.9 * 60)
        assert c.age_minutes < C.AGE_MIN_MINUTES
        ok, fails = pass_hard_filters(c)
        assert not ok
        assert any("上线" in f for f in fails)

    def test_age_5m_pass(self):
        c = _base_pass(listed_at=time.time() - 5.1 * 60)
        assert C.AGE_MIN_MINUTES <= c.age_minutes <= C.AGE_MAX_MINUTES
        ok, fails = pass_hard_filters(c)
        assert ok, fails

    def test_m5_tx_below_min_blocked(self):
        c = _base_pass(tx_count_m5=C.MIN_TX_M5 - 1)
        ok, fails = pass_hard_filters(c)
        assert not ok
        assert any("成交" in f for f in fails)

    def test_m5_volume_below_min_blocked(self):
        c = _base_pass(volume_m5_sol=C.MIN_VOLUME_M5_SOL - 0.01)
        ok, fails = pass_hard_filters(c)
        assert not ok
        assert any("成交额" in f for f in fails)
