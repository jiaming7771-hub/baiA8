"""开仓前往返报价 / 买入冲击拦截。"""

from __future__ import annotations

import pytest

from pumpfun import config as C
from pumpfun.live_swap import (
    assert_entry_liquidity,
    assert_quote_vs_ref_price,
    get_quote,
)
from pumpfun.risk import RiskBlocked


def _quote_for_price(px: float, sol_in: float = 0.05, decimals: int = 6) -> dict:
    """构造一个成交均价恰为 px 的买入报价。"""
    out_tokens = sol_in / px
    return {"outAmount": str(int(out_tokens * (10**decimals)))}


def _pin_gap(monkeypatch, *, vs_chain=True, tight=0.02, fallback=0.08):
    monkeypatch.setattr(C, "ENTRY_QUOTE_GAP_VS_CHAIN", vs_chain)
    monkeypatch.setattr(C, "ENTRY_QUOTE_MID_GAP_MAX", tight)
    monkeypatch.setattr(C, "ENTRY_QUOTE_GAP_MAX_FALLBACK", fallback)


def _stub_chain_price(monkeypatch, px):
    """替换现读链上价；px=None 模拟读取失败。"""
    import sys
    import types

    mod = types.ModuleType("pumpfun.onchain_price")

    def fetch_pool_price_sol(mint, pool=None, dex=None):
        if px is None:
            raise RuntimeError("rpc down")
        return {"price": px}

    mod.fetch_pool_price_sol = fetch_pool_price_sol
    monkeypatch.setitem(sys.modules, "pumpfun.onchain_price", mod)


def test_quote_gap_uses_fresh_chain_price_not_stale_ref(monkeypatch):
    """异源确认价便宜 5%，但报价对「现读链上价」只贵 1% → 必须放行。

    这是 MEEPCAT 被连拦两次（4.9%/6.2%）的根因：基准取了异源的确认价，
    而 gecko vs 链上价的基差实测中位就有 ~5%。
    """
    _pin_gap(monkeypatch)
    _stub_chain_price(monkeypatch, 1.0e-6)
    info = assert_quote_vs_ref_price(
        buy_quote=_quote_for_price(1.01e-6),
        sol_in=0.05,
        ref_price_sol=0.95e-6,  # 异源确认价，比链上便宜 5%
        token_mint="TOKEN",
    )
    assert info["basis"] == "chain_now"
    assert info["gap_pct"] == pytest.approx(1.0, abs=0.2)


def test_quote_gap_still_blocks_real_overpay(monkeypatch):
    """对现读链上价真的贵 5% → 照样拦，门槛没被放水。"""
    _pin_gap(monkeypatch)
    _stub_chain_price(monkeypatch, 1.0e-6)
    with pytest.raises(RiskBlocked, match="偏贵"):
        assert_quote_vs_ref_price(
            buy_quote=_quote_for_price(1.05e-6),
            sol_in=0.05,
            ref_price_sol=1.0e-6,
            token_mint="TOKEN",
        )


def test_quote_gap_falls_back_to_looser_threshold(monkeypatch):
    """读不到链上价 → 退回确认价基准，但门槛放宽到基差之上，不是拿 2% 硬打。"""
    _pin_gap(monkeypatch)
    _stub_chain_price(monkeypatch, None)
    info = assert_quote_vs_ref_price(
        buy_quote=_quote_for_price(1.05e-6),  # 相对确认价贵 5%
        sol_in=0.05,
        ref_price_sol=1.0e-6,
        token_mint="TOKEN",
    )
    assert info["basis"] == "confirm_ref"
    assert info["gap_pct"] == pytest.approx(5.0, abs=0.2)
    # 超过 fallback 门槛仍要拦
    with pytest.raises(RiskBlocked, match="偏贵"):
        assert_quote_vs_ref_price(
            buy_quote=_quote_for_price(1.12e-6),
            sol_in=0.05,
            ref_price_sol=1.0e-6,
            token_mint="TOKEN",
        )


def test_roundtrip_blocks_low_recovery(monkeypatch):
    buy_q = {"inAmount": "50000000", "outAmount": "1000000", "priceImpactPct": "0.5"}
    sell_q = {"inAmount": "1000000", "outAmount": "30000000", "priceImpactPct": "1.0"}  # 60%

    monkeypatch.setattr(C, "ROUNDTRIP_CHECK_ENABLED", True)
    monkeypatch.setattr(C, "ROUNDTRIP_MIN_RECOVERY", 0.88)
    monkeypatch.setattr(C, "ENTRY_MAX_IMPACT_PCT", 0.03)
    monkeypatch.setattr("pumpfun.live_swap.get_quote", lambda **kw: sell_q)
    with pytest.raises(RiskBlocked, match="往返|回收"):
        assert_entry_liquidity(
            token_mint="TOKEN",
            buy_quote=buy_q,
            slippage_bps=500,
            sol_in=0.05,
        )


def test_roundtrip_passes_good_recovery(monkeypatch):
    buy_q = {"inAmount": "50000000", "outAmount": "1000000", "priceImpactPct": "0.5"}
    sell_q = {"inAmount": "1000000", "outAmount": "47000000", "priceImpactPct": "0.8"}

    monkeypatch.setattr(C, "ROUNDTRIP_CHECK_ENABLED", True)
    monkeypatch.setattr(C, "ROUNDTRIP_MIN_RECOVERY", 0.88)
    monkeypatch.setattr(C, "ENTRY_MAX_IMPACT_PCT", 0.03)
    monkeypatch.setattr("pumpfun.live_swap.get_quote", lambda **kw: sell_q)

    info = assert_entry_liquidity(
        token_mint="TOKEN", buy_quote=buy_q, slippage_bps=500, sol_in=0.05
    )
    assert info["recovery"] >= 0.88


def test_entry_impact_blocks(monkeypatch):
    buy_q = {"inAmount": "50000000", "outAmount": "1000000", "priceImpactPct": "5.0"}  # 5%
    monkeypatch.setattr(C, "ENTRY_MAX_IMPACT_PCT", 0.03)
    monkeypatch.setattr(C, "ROUNDTRIP_CHECK_ENABLED", False)
    with pytest.raises(RiskBlocked, match="冲击"):
        assert_entry_liquidity(
            token_mint="TOKEN", buy_quote=buy_q, slippage_bps=500, sol_in=0.05
        )
