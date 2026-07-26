"""动量策略过滤：双轨 A/B · 回撤红线 / 老盘豁免 / 回升 / 买卖比 / 活跃度。"""

from __future__ import annotations

import time

import pytest

from pumpfun import config as C
from pumpfun.strategy import (
    Candidate,
    activity_score,
    classify_track,
    pass_hard_filters,
    pass_momentum_filters,
    pass_track_a_filters,
    pass_track_b_filters,
    score_momentum,
)


@pytest.fixture(autouse=True)
def _pin_filter_thresholds(pin_filter_defaults):
    """本文件所有用例都跑在代码默认阈值上（共享定义见 conftest）。"""


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
        # 声明数据源真给了这两个窗口；否则按新语义视为 m5/h1 顶替值，回升不采信
        chg_m15_real=True,
        chg_m30_real=True,
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

    def test_proxy_windows_never_feed_rebound(self, monkeypatch):
        """Dex 不给 m15/m30，顶替值绝不能当回升用。

        旧实现下 chg_m15 恒等于 chg_m5，「回升」实际就是 5m 涨幅。
        """
        monkeypatch.setattr(C, "REBOUND_SELF_MIN_SPAN_MIN", 10.0)
        monkeypatch.setattr(C, "REBOUND_SELF_MIN_POINTS", 6)
        c = _base_momentum(
            chg_m5=32.0,
            chg_m15=32.0,  # = chg_m5，典型顶替
            chg_m30=30.0,
            chg_m15_real=False,
            chg_m30_real=False,
        )
        assert c.rebound_src == "none"
        assert c.rebound == 0.0
        ok, fails = pass_hard_filters(c)
        assert not ok
        assert any("回升无可信来源" in f for f in fails)

    def test_rebound_from_self_collected_history(self, monkeypatch):
        """自采序列够久够密 → 按自采低点算真实回升。"""
        monkeypatch.setattr(C, "REBOUND_SELF_MIN_SPAN_MIN", 10.0)
        monkeypatch.setattr(C, "REBOUND_SELF_MIN_POINTS", 6)
        price = 1.25e-4
        c = _base_momentum(
            price=price,
            ath_price=1.3e-4,
            chg_m15_real=False,
            chg_m30_real=False,
            self_low=1.0e-4,
            self_span_min=20.0,
            self_points=40,
        )
        assert c.rebound_src == "self"
        assert c.rebound == pytest.approx(0.25, abs=0.01)
        ok, fails = pass_hard_filters(c)
        assert ok, fails

    def test_garbage_ohlcv_low_falls_back_to_self(self, monkeypatch):
        """新建池的 OHLCV 近零低点不得顶掉可信自采序列。

        实测 CAGE 回升 105122%、VORF 30551%（自采序列都是好的），
        无条件优先 OHLCV 会让这类垃圾值被回升上限拒掉 = 凭空误杀。
        """
        monkeypatch.setattr(C, "REBOUND_OHLCV_MAX_SELF_RATIO", 10.0)
        price = 1.25e-4
        c = _base_momentum(
            price=price,
            ath_price=1.3e-4,
            chg_m15_real=False,
            chg_m30_real=False,
            self_low=1.0e-4,
            self_span_min=20.0,
            self_points=40,
            ohlcv_ok=True,
            ohlcv_low=1.0e-9,  # 比自采低点低 5 个数量级 = 垃圾
        )
        assert not c.ohlcv_low_trustworthy
        assert c.rebound_src == "self"
        assert c.rebound == pytest.approx(0.25, abs=0.01)

    def test_plausible_ohlcv_low_still_wins(self, monkeypatch):
        """OHLCV 低点只是合理地更低（窗口更长）→ 仍优先采信真 K 线。"""
        monkeypatch.setattr(C, "REBOUND_OHLCV_MAX_SELF_RATIO", 10.0)
        c = _base_momentum(
            price=1.25e-4,
            ath_price=1.3e-4,
            chg_m15_real=False,
            chg_m30_real=False,
            self_low=1.0e-4,
            self_span_min=20.0,
            self_points=40,
            ohlcv_ok=True,
            ohlcv_low=5.0e-5,  # 只低 2 倍，在放宽的容忍内
        )
        assert c.ohlcv_low_trustworthy
        assert c.rebound_src == "ohlcv"
        assert c.rebound == pytest.approx(1.5, abs=0.01)

    def test_self_history_too_thin_not_trusted(self, monkeypatch):
        """序列覆盖不够久 → 不采信，而不是拿半截数据当真实低点。"""
        monkeypatch.setattr(C, "REBOUND_SELF_MIN_SPAN_MIN", 10.0)
        monkeypatch.setattr(C, "REBOUND_SELF_MIN_POINTS", 6)
        c = _base_momentum(
            chg_m15_real=False,
            chg_m30_real=False,
            self_low=1.0e-4,
            self_span_min=3.0,  # 只覆盖 3 分钟
            self_points=40,
        )
        assert c.rebound_src == "none"
        c2 = _base_momentum(
            chg_m15_real=False,
            chg_m30_real=False,
            self_low=1.0e-4,
            self_span_min=20.0,
            self_points=2,  # 点数太少
        )
        assert c2.rebound_src == "none"

    def test_wick_check_survives_proxy_windows(self, monkeypatch):
        """插针检测的分母改用自采 15m 窗口，顶替值不能再让它失效。

        旧实现分母 = max(chg_m15, chg_m30) 且 chg_m15 == chg_m5，
        比值恒 ≤ 1.0 → 该检测永不触发。
        """
        monkeypatch.setattr(C, "REBOUND_SELF_MIN_SPAN_MIN", 10.0)
        monkeypatch.setattr(C, "REBOUND_SELF_MIN_POINTS", 6)
        price = 1.2e-4
        c = _base_momentum(
            price=price,
            ath_price=1.3e-4,
            chg_m5=80.0,
            chg_m15=80.0,  # 顶替：与 chg_m5 相同
            chg_m30=75.0,
            chg_m15_real=False,
            chg_m30_real=False,
            self_low=1.0e-4,
            self_span_min=20.0,
            self_points=40,
            self_px_15m_ago=price / 1.10,  # 15m 只涨了 10%，而 5m 涨 80%
        )
        assert c.wick_base_pct == pytest.approx(10.0, abs=0.5)
        ok, fails = pass_hard_filters(c)
        assert not ok
        assert any("插针" in f for f in fails)

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


class TestActivityScore:
    """activity_s 对数刻度：旧刻度 min(1, mult/3) 在 3 倍门槛就封顶，
    实测 27/38 候选拿 20/20 满分（倍数跨 0.8~147），该分项零区分度。
    """

    @pytest.fixture(autouse=True)
    def _pin_activity_thresholds(self, monkeypatch):
        """倍数是相对门槛算的，门槛不钉住就会读本机 .env（当前 8 / 2.0）。"""
        monkeypatch.setattr(C, "MIN_TX_M5", 10)
        monkeypatch.setattr(C, "MIN_VOLUME_M5_SOL", 3.0)

    def test_at_threshold_scores_zero(self):
        """刚好卡在门槛（1.0 倍）→ 0 分，不再白送 20 分。"""
        assert activity_score(10, 3.0) == pytest.approx(0.0)

    def test_just_over_threshold_scores_low(self):
        """刚过门槛（约 1.2 倍）应拿低分，而不是旧刻度的满分。"""
        s = activity_score(12, 3.6)
        assert 0.0 < s < 0.10
        # 旧刻度在这里已经是 0.4，在 3 倍就 1.000；新刻度必须远低于此
        assert s < 0.4

    def test_old_saturation_point_no_longer_full(self):
        """回归锁定 bug 本身：3 倍门槛在旧刻度是 1.000，新刻度必须明显 < 1。"""
        s = activity_score(30, 9.0)
        assert s < 0.30

    def test_volume_spike_scores_high(self):
        """真爆量（≥ ACTIVITY_MULT_HI=100 倍）→ 满分。"""
        assert activity_score(1000, 300.0) == pytest.approx(1.0)
        # 624 倍（操作员报告的 Launches 量级）也应满分且不溢出
        assert activity_score(6240, 1872.0) == pytest.approx(1.0)

    def test_high_but_not_extreme_is_between(self):
        """10 倍门槛 → 约半分（log 刻度中点），明显区别于满分。"""
        s = activity_score(100, 30.0)
        assert s == pytest.approx(0.5, abs=0.02)

    def test_monotonic_non_decreasing(self):
        """单调不减，且在饱和前严格递增；不允许出现甜点区（先升后降）。"""
        mults = [0.5, 1.0, 1.2, 2, 3, 5, 10, 20, 50, 99, 100, 200, 1000, 5000]
        scores = [activity_score(10 * m, 3.0 * m) for m in mults]
        assert scores == sorted(scores), scores
        # 饱和(=1.0)之前必须严格递增，否则又是一个平台期
        interior = [(m, s) for m, s in zip(mults, scores) if 1.0 < m <= 100]
        for (m_a, s_a), (m_b, s_b) in zip(interior, interior[1:]):
            assert s_b > s_a, f"{m_a}->{m_b} 未严格递增: {s_a} -> {s_b}"
        assert max(scores) == pytest.approx(1.0)

    def test_short_side_governs(self):
        """仍取 tx / 成交额的短板：单边爆量不能顶起分数。"""
        # 成交额 100 倍但只有 1 倍笔数 → 按笔数算，低分
        assert activity_score(10, 300.0) == pytest.approx(0.0)
        # 反之亦然
        assert activity_score(1000, 3.0) == pytest.approx(0.0)

    def test_below_threshold_clamped_to_zero(self):
        """不足门槛不给负分。"""
        assert activity_score(0, 0.0) == 0.0
        assert activity_score(1, 0.1) == 0.0

    def test_score_momentum_discriminates_activity(self):
        """端到端：仅活跃度不同的两个候选，总分必须不同（旧代码下相同）。"""
        quiet = _base_momentum(tx_count_m5=10, volume_m5_sol=3.0)
        spike = _base_momentum(tx_count_m5=1000, volume_m5_sol=300.0)
        s_quiet, s_spike = score_momentum(quiet), score_momentum(spike)
        assert s_spike > s_quiet
        # 该分项满配 20 分，且 ohlcv_ok=False 时整体 ×0.8
        assert s_spike - s_quiet == pytest.approx(20 * 0.8, abs=0.05)
