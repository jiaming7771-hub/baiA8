"""策略过滤边界测试：跌幅 / 恐慌比 / 上线时长。"""

from __future__ import annotations

import time

import pytest

from pumpfun import config as C
from pumpfun.strategy import Candidate, pass_hard_filters


def _base_pass(**overrides) -> Candidate:
    """构造默认可通过严苛过滤的候选。"""
    now = time.time()
    ath = 1e-4
    price = ath * 0.18  # 跌幅 82%
    mid = price
    spread = 0.06
    half = mid * spread / 2.0
    kwargs = dict(
        mint="mint_test_pass_000000000000000pump",
        symbol="PASSCOIN",
        listed_at=now - 60 * 60,  # 60 分钟
        ath_price=ath,
        price=price,
        buy_vol=10.0,
        sell_vol=30.0,  # panic=3.0
        whale_dump_pct=0.80,
        bid=mid - half,
        ask=mid + half,
        liquidity_sol=20.0,
    )
    kwargs.update(overrides)
    return Candidate(**kwargs)


class TestStrategyFilters:
    def test_ath_drop_799_blocked(self):
        ath = 1.0
        price = ath * (1.0 - 0.799)  # 79.9%
        c = _base_pass(ath_price=ath, price=price, bid=price * 0.97, ask=price * 1.03)
        ok, fails = pass_hard_filters(c)
        assert not ok
        assert any("ATH" in f or "跌幅" in f for f in fails)

    def test_ath_drop_801_pass(self):
        ath = 1.0
        price = ath * (1.0 - 0.801)  # 80.1%
        mid = price
        c = _base_pass(
            ath_price=ath,
            price=price,
            bid=mid * (1 - 0.03),
            ask=mid * (1 + 0.03),
            buy_vol=10,
            sell_vol=30,
            whale_dump_pct=0.8,
            listed_at=time.time() - 45 * 60,
        )
        # ensure spread > 4%
        assert c.spread > C.SPREAD_MIN
        assert c.ath_drop >= C.ATH_DROP_MIN
        ok, fails = pass_hard_filters(c)
        assert ok, fails

    def test_panic_24_blocked(self):
        c = _base_pass(buy_vol=10.0, sell_vol=24.0)  # 2.4
        assert c.panic_ratio == pytest.approx(2.4)
        ok, fails = pass_hard_filters(c)
        assert not ok
        assert any("恐慌" in f for f in fails)

    def test_panic_26_pass(self):
        c = _base_pass(buy_vol=10.0, sell_vol=26.0)  # 2.6
        assert c.panic_ratio == pytest.approx(2.6)
        ok, fails = pass_hard_filters(c)
        assert ok, fails

    def test_age_29_blocked(self):
        c = _base_pass(listed_at=time.time() - 29 * 60)
        assert c.age_minutes < C.AGE_MIN_MINUTES
        ok, fails = pass_hard_filters(c)
        assert not ok
        assert any("上线" in f for f in fails)

    def test_age_31_pass(self):
        c = _base_pass(listed_at=time.time() - 31 * 60)
        assert C.AGE_MIN_MINUTES <= c.age_minutes <= C.AGE_MAX_MINUTES
        ok, fails = pass_hard_filters(c)
        assert ok, fails

    def test_spread_exactly_4pct_blocked(self):
        # spread 阈值是严格大于 SPREAD_MIN
        mid = 1e-5
        half = mid * C.SPREAD_MIN / 2.0
        c = _base_pass(price=mid, bid=mid - half, ask=mid + half)
        assert c.spread == pytest.approx(C.SPREAD_MIN, rel=1e-9)
        ok, fails = pass_hard_filters(c)
        assert not ok
        assert any("价差" in f for f in fails)
