"""开仓前往返报价 / 买入冲击拦截。"""

from __future__ import annotations

import pytest

from pumpfun import config as C
from pumpfun.live_swap import assert_entry_liquidity, get_quote
from pumpfun.risk import RiskBlocked


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
