"""动量策略过滤：回撤红线 / 老盘豁免 / 回升 / 买卖比 / 活跃度。"""

from __future__ import annotations

import time

import pytest

from pumpfun import config as C
from pumpfun.strategy import Candidate, pass_hard_filters, pass_momentum_filters


def _base_momentum(**overrides) -> Candidate:
    """构造默认可通过动量过滤的候选。"""
    now = time.time()
    ath = 1e-4
    pullback = 0.08  # 距高点回撤 8% ≤ 15%
    price = ath * (1.0 - pullback)
    buys_m5, sells_m5 = 30, 15  # 买/卖=2.0
    kwargs = dict(
        mint="mint_mom_pass_000000000000000pump",
        symbol="MOMCOIN",
        listed_at=now - 30 * 60,  # 30 分钟
        ath_price=ath,
        price=price,
        buy_vol=float(buys_m5),
        sell_vol=float(sells_m5),
        whale_dump_pct=0.0,
        liquidity_sol=25.0,
        tx_count_m5=buys_m5 + sells_m5,
        volume_m5_sol=8.0,
        buys_m5=buys_m5,
        sells_m5=sells_m5,
        chg_m5=5.0,
        chg_m15=32.0,  # 回升 32% ∈ [20,40]
        chg_m30=30.0,
        price_streak=2,
    )
    kwargs.update(overrides)
    return Candidate(**kwargs)


@pytest.mark.skipif(not C.IS_MOMENTUM, reason="仅 momentum 模式")
class TestMomentumFilters:
    def test_pass_baseline(self):
        ok, fails = pass_hard_filters(_base_momentum())
        assert ok, fails

    def test_age_too_young(self):
        c = _base_momentum(listed_at=time.time() - 5 * 60)
        ok, fails = pass_hard_filters(c)
        assert not ok
        assert any("上线" in f for f in fails)

    def test_age_too_old_without_exempt(self):
        c = _base_momentum(listed_at=time.time() - 150 * 60)
        ok, fails = pass_hard_filters(c)
        assert not ok
        assert any("上线" in f for f in fails)

    def test_age_old_violent_exempt_passes(self):
        """超龄但 5m≥200笔/100SOL 且买/卖≥3 → 破例放行。"""
        buys, sells = 180, 40  # ratio=4.5
        c = _base_momentum(
            listed_at=time.time() - 180 * 60,
            buys_m5=buys,
            sells_m5=sells,
            tx_count_m5=buys + sells,
            volume_m5_sol=120.0,
        )
        ok, fails = pass_hard_filters(c)
        assert ok, fails

    def test_age_old_high_volume_but_weak_bs_blocked(self):
        buys, sells = 120, 100  # ratio=1.2 < 3.0
        c = _base_momentum(
            listed_at=time.time() - 180 * 60,
            buys_m5=buys,
            sells_m5=sells,
            tx_count_m5=buys + sells,
            volume_m5_sol=150.0,
        )
        ok, fails = pass_hard_filters(c)
        assert not ok
        assert any("上线" in f for f in fails)

    def test_rebound_20_passes(self):
        c = _base_momentum(chg_m15=20.0, chg_m30=18.0)
        ok, fails = pass_hard_filters(c)
        assert ok, fails

    def test_rebound_too_weak(self):
        c = _base_momentum(chg_m15=15.0, chg_m30=12.0)
        ok, fails = pass_hard_filters(c)
        assert not ok
        assert any("回升" in f and "不足" in f for f in fails)

    def test_rebound_too_extended(self):
        c = _base_momentum(chg_m15=55.0, chg_m30=50.0)
        ok, fails = pass_hard_filters(c)
        assert not ok
        assert any("过远" in f for f in fails)

    def test_chg_m5_not_positive(self):
        c = _base_momentum(chg_m5=-1.0)
        ok, fails = pass_hard_filters(c)
        assert not ok
        assert any("转正" in f or "涨幅" in f for f in fails)

    def test_buy_sell_ratio_low(self):
        c = _base_momentum(buys_m5=12, sells_m5=10, tx_count_m5=22)
        assert c.buy_sell_ratio == pytest.approx(1.2)
        ok, fails = pass_hard_filters(c)
        assert not ok
        assert any("买/卖" in f for f in fails)

    def test_pullback_spike_like_wdog_blocked(self):
        """类似 wDog：高位插针回撤 60%+，无论成交多大一律拒。"""
        ath = 1.0
        price = ath * 0.35  # 回撤 65%
        c = _base_momentum(
            ath_price=ath,
            price=price,
            volume_m5_sol=200.0,
            tx_count_m5=300,
            buys_m5=250,
            sells_m5=50,
        )
        ok, fails = pass_hard_filters(c)
        assert not ok
        assert any("回撤" in f or "插针" in f for f in fails)

    def test_pullback_just_over_15_blocked(self):
        ath = 1.0
        price = ath * 0.84  # 回撤 16%
        c = _base_momentum(ath_price=ath, price=price)
        ok, fails = pass_hard_filters(c)
        assert not ok
        assert any("回撤" in f for f in fails)

    def test_inactive_tx_blocked(self):
        c = _base_momentum(tx_count_m5=10, buys_m5=8, sells_m5=2)
        ok, fails = pass_hard_filters(c)
        assert not ok
        assert any("成交" in f for f in fails)


def test_momentum_filter_function_direct():
    """不依赖全局模式，直接测 pass_momentum_filters。"""
    c = _base_momentum()
    ok, fails = pass_momentum_filters(c)
    assert ok, fails
