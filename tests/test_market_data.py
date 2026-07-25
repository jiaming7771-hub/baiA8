"""真实行情计算：ATH 双轨保护、m15 恐慌与 m5 活跃度。"""

from __future__ import annotations

import time

import pytest

from pumpfun import config as C
from pumpfun import market_data as M


def test_derive_ath_uses_largest_window_price():
    price = 0.4
    ath = M._derive_ath_from_changes(
        price,
        {"m5": "-10", "m15": "-20", "h1": "-60", "h6": "-50"},
    )
    assert ath == pytest.approx(1.0)


def test_derive_ath_ignores_extreme_dirty_window():
    price = 1.0
    ath = M._derive_ath_from_changes(
        price,
        {"m5": "-99.9", "m15": "-50"},
    )
    assert ath == pytest.approx(2.0)
    assert ath <= price * C.ATH_MAX_MULTIPLIER


def test_watch_peak_combines_observed_and_derived(monkeypatch):
    monkeypatch.setattr(M, "_watchlist", {})
    monkeypatch.setattr(M, "_last_prices", {})
    row = {
        "mint": "mint",
        "pool": "pool",
        "symbol": "TEST",
        "listed_at": time.time() - 600,
        "price_sol": 0.4,
        "ath_est": 1.0,
        "buys_m5": 3,
        "sells_m5": 4,
        "buys_m15": 10,
        "sells_m15": 15,
        "buyers_m15": 8,
        "sellers_m15": 6,
        "buys_h1": 20,
        "sells_h1": 30,
        "buyers_h1": 12,
        "sellers_h1": 10,
        "chg_m5": -5.0,
        "vol_m5_usd": 148.0,
        "vol_m5_sol": 2.0,
        "liquidity_sol": 20.0,
    }
    M._update_watch_entry(row)
    assert M._watchlist["mint"]["peak_price"] == pytest.approx(1.0)

    # 后续真实观测高点高于反推值时，观测 peak 胜出。
    row.update(price_sol=1.2, ath_est=1.1)
    M._update_watch_entry(row)
    assert M._watchlist["mint"]["peak_price"] == pytest.approx(1.2)


def test_build_candidates_uses_m15_and_m5(monkeypatch):
    monkeypatch.setattr(
        M,
        "_watchlist",
        {
            "mint": {
                "mint": "mint",
                "symbol": "TEST",
                "listed_at": time.time() - 30 * 60,
                "price_sol": 0.4,
                "peak_price": 1.0,
                "buys_m5": 3,
                "sells_m5": 4,
                "vol_m5_sol": 2.0,
                "vol_m5_usd": 148.0,
                "buys_m15": 10,
                "sells_m15": 15,
                "sellers_m15": 6,
                # h1 故意给相反信号，确保不再使用它
                "buys_h1": 100,
                "sells_h1": 1,
                "sellers_h1": 1,
                "liquidity_sol": 20.0,
            }
        },
    )
    candidate = M.build_candidates()[0]
    assert candidate.panic_ratio == pytest.approx(1.5)
    assert candidate.whale_dump_pct == pytest.approx(0.6)
    assert candidate.tx_count_m5 == 7
    assert candidate.volume_m5_sol == pytest.approx(2.0)
    assert candidate.volume_m5_usd == pytest.approx(148.0)
