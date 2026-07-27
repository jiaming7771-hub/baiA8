"""行情管道 Part A：多池选主、换池重置、链上报价刷新（不含策略门槛改动）。"""

from __future__ import annotations

import time

import pytest

from pumpfun import config as C
from pumpfun import market_data as M


def _raw_pair(
    *,
    mint: str,
    pool: str,
    dex_id: str,
    liq_usd: float,
    vol_m5: float,
    price_native: float = 0.0001,
    quote: str | None = None,
) -> dict:
    quote = quote or C.SOL_MINT
    return {
        "chainId": "solana",
        "dexId": dex_id,
        "pairAddress": pool,
        "pairCreatedAt": int((time.time() - 600) * 1000),
        "priceNative": str(price_native),
        "priceUsd": str(price_native * 100.0),
        "baseToken": {"address": mint, "symbol": "T"},
        "quoteToken": {"address": quote, "symbol": "SOL"},
        "txns": {"m5": {"buys": 10, "sells": 4}, "h1": {"buys": 100, "sells": 40}},
        "volume": {"m5": vol_m5, "h1": vol_m5 * 10},
        "priceChange": {"m5": 5.0, "h1": 20.0},
        "liquidity": {"usd": liq_usd},
    }


@pytest.fixture(autouse=True)
def _sol_cache():
    M._sol_usd = 100.0
    M._sol_usd_ts = time.time()
    yield


def test_picker_prefers_pumpswap_over_dead_dbc():
    """meteoradbc 0/0 + pumpswap 深池 → 选 pumpswap；仅 0/0 → 空。"""
    mint = "MintHot"
    pairs = [
        _raw_pair(
            mint=mint,
            pool="dbcPool",
            dex_id="meteoradbc",
            liq_usd=0.0,
            vol_m5=0.0,
            price_native=4.5e-15,
        ),
        _raw_pair(
            mint=mint,
            pool="swapPool",
            dex_id="pumpswap",
            liq_usd=371_000.0,
            vol_m5=50_000.0,
        ),
    ]
    rows = M._pick_best_dex_pairs(pairs)
    assert len(rows) == 1
    assert rows[0]["pool"] == "swapPool"
    assert rows[0]["dex"] == "pumpswap"

    only_dead = M._pick_best_dex_pairs(pairs[:1])
    assert only_dead == []


def test_dex_fetch_continues_after_mid_chunk_error(monkeypatch):
    """中间块报错不得拖死后续块（旧逻辑是 break）。"""
    monkeypatch.setattr(C, "DEX_BATCH_SIZE", 2)
    monkeypatch.setattr(M, "DEX_BATCH_SIZE", 2)
    mints = ["m0", "m1", "m2", "m3", "m4", "m5"]
    calls: list[list[str]] = []

    def fake_get(url, **_kw):
        addrs = url.rsplit("/", 1)[-1].split(",")
        calls.append(addrs)
        if "m2" in addrs:
            raise M.MarketDataError("boom mid")
        return {
            "pairs": [
                _raw_pair(
                    mint=a,
                    pool=f"p_{a}",
                    dex_id="pumpswap",
                    liq_usd=20_000.0,
                    vol_m5=1_000.0,
                )
                for a in addrs
            ]
        }

    monkeypatch.setattr(M, "_get_json", fake_get)
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    rows = M._dex_fetch_token_rows(mints)
    got = {r["mint"] for r in rows}
    assert "m0" in got and "m1" in got
    assert "m2" not in got and "m3" not in got  # 失败块整块缺席
    assert "m4" in got and "m5" in got  # 后续块仍在
    assert len(calls) == 3


def test_pool_switch_resets_hist_peak_streak(monkeypatch):
    """换池必须清空 px_hist / 重置 peak / streak，否则 108× 跳变永久毒化回升。"""
    monkeypatch.setattr(M, "_watchlist", {})
    monkeypatch.setattr(M, "_last_prices", {})
    now = time.time()
    M._watchlist["mintX"] = {
        "mint": "mintX",
        "pool": "oldPool",
        "dex": "pumpswap",
        "symbol": "X",
        "listed_at": now - 600,
        "peak_price": 1.0,
        "price_streak": 7,
        "price_sol": 0.5,
        "px_hist": [[now - 120, 0.4], [now - 60, 0.5]],
        "buys_m5": 1,
        "sells_m5": 1,
        "buys_m15": 1,
        "sells_m15": 1,
        "buyers_m15": 1,
        "sellers_m15": 1,
        "buys_h1": 1,
        "sells_h1": 1,
        "buyers_h1": 1,
        "sellers_h1": 1,
        "chg_m5": 0,
        "vol_m5_usd": 1,
        "vol_m5_sol": 0.01,
        "liquidity_sol": 20,
    }
    new_px = 54.0  # ~108× old
    row = {
        "mint": "mintX",
        "pool": "newPool",
        "dex": "pumpswap",
        "symbol": "X",
        "listed_at": now - 600,
        "price_sol": new_px,
        "ath_est": new_px,
        "buys_m5": 2,
        "sells_m5": 1,
        "buys_m15": 6,
        "sells_m15": 3,
        "buyers_m15": 4,
        "sellers_m15": 2,
        "buys_h1": 20,
        "sells_h1": 10,
        "buyers_h1": 8,
        "sellers_h1": 5,
        "chg_m5": 5.0,
        "chg_m15": 5.0,
        "chg_m30": 10.0,
        "chg_m15_real": False,
        "chg_m30_real": False,
        "vol_m5_usd": 100.0,
        "vol_m5_sol": 1.0,
        "liquidity_sol": 200.0,
        "source": "dex",
    }
    M._update_watch_entry(row)
    ent = M._watchlist["mintX"]
    assert ent["pool"] == "newPool"
    assert ent["peak_price"] == pytest.approx(new_px)
    assert ent["price_streak"] == 1
    assert ent.get("pool_switched_at", 0) > 0
    # 旧池样本不得残留；新样本可入列
    hist = ent.get("px_hist") or []
    assert all(
        (not isinstance(s, (list, tuple)) or len(s) < 4 or s[3] == "newPool")
        for s in hist
    )
    assert not any(
        isinstance(s, (list, tuple)) and len(s) >= 2 and float(s[1]) == 0.5
        for s in hist
    )


def test_onchain_refresh_guards(monkeypatch):
    """链上刷新：拒 1e-18、不碰 updated、不覆盖更新 Dex 样本、丢弃错池。"""
    monkeypatch.setattr(C, "ONCHAIN_WATCH_REFRESH", True)
    monkeypatch.setattr(C, "ONCHAIN_REFRESH_MIN_INTERVAL_SEC", 0.0)
    monkeypatch.setattr(C, "ONCHAIN_WATCH_MAX_POOLS", 120)
    monkeypatch.setattr(C, "PX_HIST_MIN_GAP_SEC", 10.0)
    monkeypatch.setattr(M, "_last_onchain_watch_refresh", 0.0)
    monkeypatch.setattr(M, "_onchain_px_pending", {})
    monkeypatch.setattr(M, "_load_watchlist", lambda: None)
    monkeypatch.setattr(M, "_last_prices", {})
    now = time.time()
    M._watchlist.clear()
    M._watchlist["m1"] = {
        "mint": "m1",
        "pool": "poolA",
        "dex": "pumpswap",
        "symbol": "A",
        "listed_at": now - 600,
        "price_sol": 1.0,
        "peak_price": 1.0,
        "price_streak": 1,
        "px_hist": [[now - 30, 1.0, "d", "poolA"]],
        "px_ts": now - 30,
        "updated": now - 100,
        "liquidity_sol": 50.0,
    }
    M._watchlist["m2"] = {
        "mint": "m2",
        "pool": "poolB",
        "dex": "pumpswap",
        "symbol": "B",
        "listed_at": now - 600,
        "price_sol": 2.0,
        "peak_price": 2.0,
        "price_streak": 1,
        "px_hist": [[now - 1, 2.0, "d", "poolB"]],  # 更新于 gap 内
        "px_ts": now - 1,
        "updated": now - 50,
        "liquidity_sol": 50.0,
    }
    M._watchlist["m3"] = {
        "mint": "m3",
        "pool": "poolC",
        "dex": "pumpswap",
        "symbol": "C",
        "listed_at": now - 600,
        "price_sol": 3.0,
        "peak_price": 3.0,
        "price_streak": 1,
        "px_hist": [],
        "px_ts": now - 30,
        "updated": now - 80,
        "liquidity_sol": 50.0,
    }

    snaps = {
        "poolA": {"price": 1e-18, "reason": "vault_drained", "sol_vault": 0.0},
        "poolB": {"price": 2.05, "reason": "", "sol_vault": 40.0},
        "poolC": {"price": 3.1, "reason": "", "sol_vault": 40.0},
    }

    def fake_snaps(pools):
        return {p: snaps[p] for p in pools if p in snaps}

    monkeypatch.setattr(M.onchain, "batch_pool_snapshots", fake_snaps)
    monkeypatch.setattr(M.rpc, "would_exceed_budget", lambda _n: False)

    n = M.refresh_watchlist_prices_onchain()
    assert M._watchlist["m1"]["updated"] == pytest.approx(now - 100)
    hist1 = M._watchlist["m1"]["px_hist"]
    assert not any(float(s[1]) <= 1e-17 for s in hist1 if len(s) >= 2)
    # m2：Dex 样本仍新鲜 → 不得被链上覆盖
    assert M._watchlist["m2"]["px_hist"][-1][1] == pytest.approx(2.0)
    assert M._watchlist["m2"]["updated"] == pytest.approx(now - 50)
    # m3：正常写入
    assert n == 1
    assert M._watchlist["m3"]["price_sol"] == pytest.approx(3.1)
    assert M._watchlist["m3"]["updated"] == pytest.approx(now - 80)
    assert M._watchlist["m3"]["px_hist"][-1][2] == "c"

    # 错池：选池后、写回前 entry.pool 被换掉 → 样本丢弃
    M._last_onchain_watch_refresh = 0.0
    M._watchlist["m3"]["px_ts"] = time.time() - 30
    M._watchlist["m3"]["price_sol"] = 3.0
    before_hist_len = len(M._watchlist["m3"]["px_hist"])

    def snaps_with_switch(pools):
        M._watchlist["m3"]["pool"] = "poolSwitched"
        return {p: snaps[p] for p in pools if p in snaps}

    monkeypatch.setattr(M.onchain, "batch_pool_snapshots", snaps_with_switch)
    n2 = M.refresh_watchlist_prices_onchain()
    assert n2 == 0
    assert M._watchlist["m3"]["price_sol"] == pytest.approx(3.0)
    assert len(M._watchlist["m3"]["px_hist"]) == before_hist_len


def test_px_hist_stats_accepts_legacy_two_tuples():
    """旧 live_watchlist 两元组样本仍参与统计。"""
    now = time.time()
    ent = {
        "pool": "poolX",
        "px_hist": [
            [now - 20 * 60, 1.0],
            [now - 16 * 60, 0.5],
            [now - 60, 2.0, "d", "poolX"],
            [now - 30, 9.0, "c", "otherPool"],  # 异池，应忽略
        ],
    }
    st = M.px_hist_stats(ent)
    assert st["points"] == 3
    assert st["low"] == pytest.approx(0.5)
    assert st["high"] == pytest.approx(2.0)
    assert st["px_15m_ago"] == pytest.approx(0.5)
