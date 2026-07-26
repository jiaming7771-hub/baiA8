"""动量策略过滤：双轨 A/B · 回撤红线 / 老盘豁免 / 回升 / 买卖比 / 活跃度。"""

from __future__ import annotations

import time

import pytest

from pumpfun import config as C
from pumpfun.strategy import (
    Candidate,
    classify_track,
    pass_hard_filters,
    pass_momentum_filters,
    pass_track_a_filters,
    pass_track_b_filters,
)


@pytest.fixture(autouse=True)
def _pin_filter_thresholds(monkeypatch):
    """过滤阈值钉到代码默认值。

    本机 .env 常年跑「测试宽松档」，直接读会让这些用例断言的是操作员当下的
    调参结果而非过滤逻辑本身。
    """
    defaults = {
        "TRACK_A_AGE_MIN": 5.0,
        "TRACK_A_AGE_MAX": 120.0,
        "TRACK_A_REBOUND_MIN": 0.15,
        "TRACK_A_REBOUND_MAX": 0.80,
        "TRACK_A_PULLBACK_MAX": 0.20,
        "TRACK_A_LIQ_MIN": 10.0,
        "TRACK_A_MIN_TX_M5": 10,
        "TRACK_A_MIN_VOL_M5": 3.0,
        "TRACK_A_BUY_SELL_MIN": 1.3,
        "TRACK_B_ENABLED": True,
        "TRACK_B_AGE_MIN": 45.0,
        "TRACK_B_AGE_MAX": 1440.0,
        "TRACK_B_LIQ_MIN": 30.0,
        "TRACK_B_PULLBACK_MAX": 0.08,
        "TRACK_B_MIN_TX_M5": 15,
        "TRACK_B_MIN_VOL_M5": 8.0,
        "TRACK_B_BUY_SELL_MIN": 1.5,
        "TRACK_B_VOL_SPIKE_RATIO": 2.5,
        "REBOUND_STRICT_FROM": 0.40,
        "REBOUND_STRICT_BUY_SELL": 2.0,
        "REBOUND_STRICT_PULLBACK": 0.08,
        "CRASH_PULLBACK_MAX": 0.30,
        "MDD_BLACKLIST_PCT": 0.50,
        "MOMENTUM_STREAK_MIN": 1,
        "WICK_SPIKE_RATIO": 2.5,
        "AGE_EXEMPT_VOLUME_M5_SOL": 100.0,
        "AGE_EXEMPT_TX_M5": 200,
        "AGE_EXEMPT_BUY_SELL_RATIO": 3.0,
        "ENTRY_CHG_M5_MIN": 3.0,
        "ENTRY_CHG_M5_MAX": 25.0,
    }
    for name, value in defaults.items():
        monkeypatch.setattr(C, name, value)


def _base_momentum(**overrides) -> Candidate:
    """构造默认可通过轨道 A 的候选。"""
    now = time.time()
    ath = 1e-4
    pullback = 0.08  # 距高点回撤 8% ≤ 20%
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
        chg_m15=32.0,  # 回升 32% ∈ [15,80]
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
        track, _ = classify_track(_base_momentum())
        assert track == "A"

    def test_age_too_young(self):
        c = _base_momentum(listed_at=time.time() - 1 * 60)
        ok, fails = pass_hard_filters(c)
        assert not ok
        assert any("上线" in f for f in fails)

    def test_age_too_old_without_exempt(self):
        """150m：A 超龄且未豁免；B 因流动性不足也不过。"""
        c = _base_momentum(listed_at=time.time() - 150 * 60, liquidity_sol=25.0)
        ok, fails = pass_hard_filters(c)
        assert not ok
        assert any(("上线" in f) or ("流动性" in f) for f in fails)

    def test_track_b_passes_old_shallow(self):
        """老盘：浅回撤 + 放量节奏 → 走轨道 B。"""
        buys, sells = 40, 20
        c = _base_momentum(
            listed_at=time.time() - 200 * 60,
            liquidity_sol=40.0,
            ath_price=1.0,
            price=0.95,  # 回撤 5% ≤ 8%
            buys_m5=buys,
            sells_m5=sells,
            tx_count_m5=buys + sells,
            volume_m5_sol=20.0,
            volume_h1_sol=40.0,  # pace=20*12=240 ≥ 40*2.5
            chg_m5=3.0,
            chg_m15=5.0,
            chg_m30=4.0,
        )
        track, fails = classify_track(c)
        assert track == "B", fails

    def test_age_old_violent_exempt_passes(self):
        """超龄但 5m≥200笔/100SOL 且买/卖≥3 → A 轨破例放行。"""
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
        track, _ = classify_track(c)
        assert track == "A"

    def test_age_old_high_volume_but_weak_bs_blocked(self):
        buys, sells = 120, 100  # ratio=1.2 < 3.0
        c = _base_momentum(
            listed_at=time.time() - 180 * 60,
            buys_m5=buys,
            sells_m5=sells,
            tx_count_m5=buys + sells,
            volume_m5_sol=150.0,
            liquidity_sol=25.0,  # B 也不够
        )
        ok, fails = pass_hard_filters(c)
        assert not ok
        assert any(("上线" in f) or ("流动性" in f) or ("买/卖" in f) for f in fails)

    def test_rebound_20_passes(self):
        c = _base_momentum(chg_m15=22.0, chg_m30=20.0)
        ok, fails = pass_hard_filters(c)
        assert ok, fails

    def test_wick_spike_blocked(self):
        """5m 暴涨远超 15/30m → 插针假反弹拦截。"""
        c = _base_momentum(chg_m5=80.0, chg_m15=25.0, chg_m30=22.0)
        ok, fails = pass_hard_filters(c)
        assert not ok
        assert any("插针" in f for f in fails)

    def test_single_window_blocked_without_ohlcv(self):
        """无真实K线时要求 m15/m30 双窗口同向为正。"""
        c = _base_momentum(chg_m15=30.0, chg_m30=-5.0)
        ok, fails = pass_hard_filters(c)
        assert not ok
        assert any("双窗口" in f for f in fails)

    def test_ohlcv_rebound_uses_real_low(self):
        """有 OHLCV 时按真实 low 算回升。"""
        c = _base_momentum(
            price=1.25e-4,
            ath_price=1.3e-4,
            ohlcv_low=1.0e-4,
            ohlcv_high=1.3e-4,
            ohlcv_ok=True,
            chg_m15=5.0,  # 反推会被忽略
            chg_m30=5.0,
        )
        # rebound = 1.25/1.0 - 1 = 25%
        assert c.rebound == pytest.approx(0.25, abs=0.01)
        ok, fails = pass_hard_filters(c)
        assert ok, fails

    def test_stale_signal_blocked(self):
        c = _base_momentum(data_ts=time.time() - 500)
        ok, fails = pass_hard_filters(c)
        assert not ok
        assert any("过旧" in f for f in fails)

    def test_drawdown_clamped_when_ath_below_price(self):
        """脏 ATH < 现价时回撤必须是 0，绝不是 -600%。"""
        c = _base_momentum(
            price=1.3e-8,
            ath_price=1.7e-9,  # 错误地低于现价
        )
        assert c.drawdown == 0.0
        assert c.pullback == 0.0
        row = c.to_row()
        assert row["drawdown_pct"] == 0.0
        assert row["pullback_pct"] == 0.0
        assert -100.0 <= row["drawdown_pct"] <= 0.0

    def test_drawdown_signed_range(self):
        c = _base_momentum(ath_price=1.0, price=0.85)
        assert c.drawdown == pytest.approx(-0.15, abs=1e-6)
        assert c.to_row()["drawdown_pct"] == pytest.approx(-15.0, abs=0.05)

    def test_crash_pullback_veto(self):
        """回撤 >30% 砸盘残废一票否决。"""
        c = _base_momentum(ath_price=1.0, price=0.60)  # -40%
        ok, fails = pass_hard_filters(c)
        assert not ok
        assert any("砸盘" in f or "回撤" in f for f in fails)

    def test_clone_symbol_blocked(self):
        from pumpfun.strategy import sanitize_candidates

        clones = [
            _base_momentum(mint="mint_btc_clone_000000000000pump", symbol="BTC"),
            _base_momentum(mint="mint_sol2_clone_00000000000pump", symbol="SOL2"),
        ]
        real = _base_momentum(mint="mint_real_aorp_000000000000pump", symbol="AORP")
        out = sanitize_candidates(clones + [real])
        syms = {c.symbol for c in out}
        assert "BTC" not in syms
        assert "SOL2" not in syms
        assert "AORP" in syms

    def test_duplicate_symbol_keeps_best_mint(self):
        from pumpfun.strategy import sanitize_candidates

        weak = _base_momentum(
            mint="mint_aorp_weak_000000000000pump",
            symbol="AORP",
            liquidity_sol=5.0,
            volume_m5_sol=1.0,
        )
        strong = _base_momentum(
            mint="mint_aorp_strong_0000000000pump",
            symbol="AORP",
            liquidity_sol=50.0,
            volume_m5_sol=20.0,
        )
        out = sanitize_candidates([weak, strong])
        assert len(out) == 1
        assert out[0].mint.startswith("mint_aorp_strong")

    def test_rebound_too_weak(self):
        c = _base_momentum(chg_m15=10.0, chg_m30=8.0)  # < 15%
        ok, fails = pass_hard_filters(c)
        assert not ok
        assert any("回升" in f for f in fails)

    def test_rebound_too_extended(self):
        c = _base_momentum(chg_m15=85.0, chg_m30=82.0)  # > 80%
        ok, fails = pass_hard_filters(c)
        assert not ok
        assert any("回升" in f for f in fails)

    def test_rebound_soft_zone_passes_with_strict_gates(self):
        """回升 55%：买/卖≥2 且回撤≤8% → 放行。"""
        ath = 1.0
        price = ath * 0.95  # 回撤 5%
        buys, sells = 40, 15  # 2.67
        c = _base_momentum(
            ath_price=ath,
            price=price,
            chg_m15=55.0,
            chg_m30=50.0,
            buys_m5=buys,
            sells_m5=sells,
            tx_count_m5=buys + sells,
        )
        ok, fails = pass_hard_filters(c)
        assert ok, fails

    def test_rebound_soft_zone_blocked_without_bs(self):
        ath = 1.0
        price = ath * 0.95
        buys, sells = 20, 15  # 1.33 < 2.0
        c = _base_momentum(
            ath_price=ath,
            price=price,
            chg_m15=55.0,
            chg_m30=50.0,
            buys_m5=buys,
            sells_m5=sells,
            tx_count_m5=buys + sells,
        )
        ok, fails = pass_hard_filters(c)
        assert not ok
        assert any("延伸" in f and "买/卖" in f for f in fails)

    def test_rebound_soft_zone_blocked_without_tight_pullback(self):
        ath = 1.0
        price = ath * 0.88  # 回撤 12% > 8%
        buys, sells = 40, 15
        c = _base_momentum(
            ath_price=ath,
            price=price,
            chg_m15=55.0,
            chg_m30=50.0,
            buys_m5=buys,
            sells_m5=sells,
            tx_count_m5=buys + sells,
        )
        ok, fails = pass_hard_filters(c)
        assert not ok
        assert any("延伸" in f and "回撤" in f for f in fails)

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

    def test_pullback_just_over_a_max_blocked(self):
        ath = 1.0
        price = ath * 0.79  # 回撤 21% > A 轨 20%
        c = _base_momentum(ath_price=ath, price=price)
        ok, fails = pass_hard_filters(c)
        assert not ok
        assert any("回撤" in f for f in fails)

    def test_inactive_tx_blocked(self):
        c = _base_momentum(tx_count_m5=8, buys_m5=6, sells_m5=2)
        ok, fails = pass_hard_filters(c)
        assert not ok
        assert any("成交" in f for f in fails)


def test_momentum_filter_function_direct():
    """不依赖全局模式，直接测 pass_momentum_filters / pass_track_a。"""
    c = _base_momentum()
    ok, fails = pass_momentum_filters(c)
    assert ok, fails
    ok_a, fails_a = pass_track_a_filters(c)
    assert ok_a, fails_a


def test_track_b_filter_direct():
    buys, sells = 40, 20
    c = _base_momentum(
        listed_at=time.time() - 300 * 60,
        liquidity_sol=50.0,
        ath_price=1.0,
        price=0.96,
        buys_m5=buys,
        sells_m5=sells,
        tx_count_m5=buys + sells,
        volume_m5_sol=25.0,
        volume_h1_sol=50.0,
        chg_m5=5.0,
        chg_m15=3.0,
        chg_m30=2.5,
    )
    ok, fails = pass_track_b_filters(c)
    assert ok, fails


def test_chg_m5_window_rejects_cold_and_overheated():
    """5m 涨幅窗口：动能不足与过热追高都拦。"""
    cold = _base_momentum(chg_m5=1.0)
    ok, fails = pass_hard_filters(cold)
    assert not ok
    assert any("动能不足" in f for f in fails)

    hot = _base_momentum(chg_m5=40.0, chg_m15=38.0, chg_m30=36.0)
    ok, fails = pass_hard_filters(hot)
    assert not ok
    assert any("过热追高" in f for f in fails)
