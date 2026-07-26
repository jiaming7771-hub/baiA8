"""抽池/假盘口价场景：报价兑现校验必须拦住「按假价砸盘」的卖单。"""

from __future__ import annotations

import pytest

from pumpfun import config as C
from pumpfun import live_swap as LS


@pytest.fixture
def fake_chain(monkeypatch):
    """屏蔽真实链上交互，只保留报价 → 兑现校验这条逻辑。"""
    sent: list[dict] = []

    monkeypatch.setattr(LS, "build_swap_tx", lambda quote, pubkey: "raw_tx")
    monkeypatch.setattr(LS, "sign_versioned_tx", lambda raw: "signed_tx")
    monkeypatch.setattr(
        LS,
        "send_and_confirm",
        lambda signed: sent.append({"signed": signed})
        or {"signature": "SIG" * 10, "elapsed_sec": 0.4},
    )
    monkeypatch.setattr(
        LS, "fetch_actual_fill", lambda **kw: {"ok": False}
    )
    return sent


def _quote_returning(lamports: int):
    def _q(**kwargs):
        return {"outAmount": str(lamports), "inAmount": "1", "priceImpactPct": "0.9"}

    return _q


def test_sell_blocked_when_quote_cannot_cover_mark(monkeypatch, fake_chain):
    """盘口估值 0.026 SOL，报价只兑现 0.0006 SOL → 拒绝成交。"""
    monkeypatch.setattr(LS, "get_quote", _quote_returning(590_000))  # 0.00059 SOL

    with pytest.raises(LS.LiquidityCollapse):
        LS._sell_once(
            token_mint="MintX",
            token_amount_raw=12_530_358_265_318,
            decimals=6,
            bps=500,
            pubkey="Owner",
            routing="default",
            expect_sol=0.026,
        )
    assert not fake_chain, "坍塌时不应广播任何交易"


def test_sell_allowed_within_impact_budget(monkeypatch, fake_chain):
    """缩水在阈值内（约 -20%）→ 正常成交。"""
    monkeypatch.setattr(LS, "get_quote", _quote_returning(20_800_000))  # 0.0208 SOL

    out = LS._sell_once(
        token_mint="MintX",
        token_amount_raw=1_000_000,
        decimals=6,
        bps=500,
        pubkey="Owner",
        routing="default",
        expect_sol=0.026,
    )
    assert out["sol_amount"] == pytest.approx(0.0208)
    assert len(fake_chain) == 1


def test_force_sell_executes_despite_collapse(monkeypatch, fake_chain):
    """保命单强制模式：明知巨额折价也要成交止血。"""
    monkeypatch.setattr(LS, "get_quote", _quote_returning(590_000))

    out = LS._sell_once(
        token_mint="MintX",
        token_amount_raw=12_530_358_265_318,
        decimals=6,
        bps=1000,
        pubkey="Owner",
        routing="open",
        expect_sol=0.026,
        force=True,
    )
    assert out["sol_amount"] == pytest.approx(0.00059)
    assert len(fake_chain) == 1


def test_no_expectation_skips_guard(monkeypatch, fake_chain):
    """没有盘口估值参照时不做校验，避免误伤。"""
    monkeypatch.setattr(LS, "get_quote", _quote_returning(1_000))

    out = LS._sell_once(
        token_mint="MintX",
        token_amount_raw=1_000_000,
        decimals=6,
        bps=500,
        pubkey="Owner",
        routing="default",
        expect_sol=0.0,
    )
    assert out["sol_amount"] == pytest.approx(0.000001)


def test_impact_threshold_from_config():
    assert 0.05 <= C.EXIT_MAX_IMPACT_PCT <= 0.95
