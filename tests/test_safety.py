"""买入前链上安全审计：貔貅/增发/撤池拦截 + fail-closed。"""

from __future__ import annotations

import base64
import struct

import pytest

from pumpfun import holders
from pumpfun import safety
from pumpfun.onchain_price import PUMP_PROGRAM, PUMPSWAP_PROGRAM

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
GOOD_MINT = "So11111111111111111111111111111111111111112"
AUTH = "5DR3ChhwwEy6pLSkAj9tfrnU5z6gzm8rnCEk2tsgkViG"


def _mint_value(*, mint_authority, freeze_authority, owner=TOKEN_PROGRAM):
    return {
        "owner": owner,
        "data": {
            "program": "spl-token",
            "parsed": {
                "type": "mint",
                "info": {
                    "mintAuthority": mint_authority,
                    "freezeAuthority": freeze_authority,
                    "decimals": 6,
                    "supply": "1000000000000000",
                    "isInitialized": True,
                },
            },
        },
    }


@pytest.fixture(autouse=True)
def _clear_cache():
    safety.clear_cache()
    holders.clear_cache()
    yield
    safety.clear_cache()
    holders.clear_cache()


def _patch(monkeypatch, mint_value, pool_owner, *, holder_ok=True, mint_addr="MINT"):
    def fake_get_account_info(pubkey, *, encoding="base64", commitment="confirmed"):
        if pubkey == "POOL":
            return {"owner": pool_owner, "data": ["", "base64"]} if pool_owner else None
        if pubkey == mint_addr:
            return mint_value
        # 元数据 PDA / 其它：视为无账户（updateAuthority 检查放行）
        return None

    monkeypatch.setattr(safety.rpc, "get_account_info", fake_get_account_info)
    # 默认 stub 筹码审计，避免旧测试打真 RPC
    monkeypatch.setattr(
        holders,
        "check_holder_concentration",
        lambda mint, **kw: holders.HolderResult(
            ok=holder_ok,
            reasons=[] if holder_ok else ["筹码过度集中/老鼠仓控盘 (>40%)"],
            checks={"top_pct": 0.2 if holder_ok else 0.55},
            whale_snapshot={"W1": 1000} if holder_ok else {},
        ),
    )


def test_pass_when_authorities_renounced_and_pumpswap_pool(monkeypatch):
    _patch(
        monkeypatch,
        _mint_value(mint_authority=None, freeze_authority=None),
        PUMPSWAP_PROGRAM,
    )
    r = safety.check_token_safety("MINT", pool="POOL", dex="pumpswap")
    assert r.ok, r.reasons


def test_block_when_freeze_authority_present(monkeypatch):
    _patch(
        monkeypatch,
        _mint_value(mint_authority=None, freeze_authority=AUTH),
        PUMPSWAP_PROGRAM,
    )
    r = safety.check_token_safety("MINT", pool="POOL", dex="pumpswap")
    assert not r.ok
    assert any("freeze" in x for x in r.reasons)


def test_block_when_mint_authority_present(monkeypatch):
    _patch(
        monkeypatch,
        _mint_value(mint_authority=AUTH, freeze_authority=None),
        PUMP_PROGRAM,
    )
    r = safety.check_token_safety("MINT", pool="POOL", dex="pump-fun")
    assert not r.ok
    assert any("mint_authority" in x for x in r.reasons)


def test_block_unknown_pool_program(monkeypatch):
    _patch(
        monkeypatch,
        _mint_value(mint_authority=None, freeze_authority=None),
        "RaydiumUnknownProgram1111111111111111111111",
    )
    r = safety.check_token_safety("MINT", pool="POOL", dex="raydium")
    assert not r.ok
    assert any("撤池" in x or "未知程序" in x for x in r.reasons)


def test_fail_closed_on_rpc_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("RPC timeout")

    monkeypatch.setattr(safety.rpc, "get_account_info", boom)
    r = safety.check_token_safety("MINT", pool="POOL", dex="pumpswap")
    assert not r.ok
    assert any("超时" in x or "RPC" in x for x in r.reasons)


def test_fail_closed_when_pool_missing(monkeypatch):
    _patch(
        monkeypatch,
        _mint_value(mint_authority=None, freeze_authority=None),
        None,  # 池账户不存在
    )
    r = safety.check_token_safety("MINT", pool="POOL", dex="pumpswap")
    assert not r.ok


def test_base64_mint_layout_parsed(monkeypatch):
    """无 jsonParsed 时回退 base64：构造放弃权限的 SPL Mint。"""
    raw = bytearray(82)
    struct.pack_into("<I", raw, 0, 0)  # mint_authority option = 0（无）
    struct.pack_into("<Q", raw, 36, 10**15)  # supply
    raw[44] = 6  # decimals
    raw[45] = 1  # is_initialized
    struct.pack_into("<I", raw, 46, 0)  # freeze option = 0（无）
    value = {"owner": TOKEN_PROGRAM, "data": [base64.b64encode(bytes(raw)).decode(), "base64"]}
    _patch(monkeypatch, value, PUMPSWAP_PROGRAM)
    r = safety.check_token_safety("MINT", pool="POOL", dex="pumpswap")
    assert r.ok, r.reasons


def test_open_long_blocked_by_safety(monkeypatch):
    """实盘 open_long：安全审计不通过时必须拒绝下单，且不触碰 Jupiter。"""
    from pumpfun import config as C
    from pumpfun import execution
    from pumpfun.execution import PaperBroker

    monkeypatch.setattr(C, "SAFETY_CHECK_ENABLED", True)
    monkeypatch.setattr(C, "MICRO_LIVE", True)

    broker = PaperBroker()
    broker.positions.clear()
    broker.dry_run = False
    broker.shadow = False

    # 钱包未持有该币（跳过重复买入拦截）
    monkeypatch.setattr(execution, "journal", execution.journal)
    monkeypatch.setattr(
        "pumpfun.chain.keypair_for_live",
        lambda: type("KP", (), {"pubkey": lambda self: "Owner"})(),
    )
    monkeypatch.setattr("pumpfun.rpc.get_token_balance_raw", lambda owner, mint: (0, 6))

    # 安全审计判定不通过
    monkeypatch.setattr(
        safety,
        "check_token_safety",
        lambda mint, **kw: safety.SafetyResult(ok=False, reasons=["freeze 未放弃"]),
    )

    def _boom_buy(*a, **k):
        raise AssertionError("安全未通过却触发了买入")

    monkeypatch.setattr("pumpfun.live_swap.buy_token_with_sol", _boom_buy)

    signal = {"mint": "MINTX", "symbol": "SCAM", "price": 1e-6, "pool": "POOL", "dex": "pumpswap"}
    assert broker.open_long(signal) is None


def test_cache_avoids_second_rpc(monkeypatch):
    calls = {"n": 0}

    def counting(pubkey, *, encoding="base64", commitment="confirmed"):
        calls["n"] += 1
        if pubkey == "POOL":
            return {"owner": PUMPSWAP_PROGRAM, "data": ["", "base64"]}
        if pubkey == "MINT":
            return _mint_value(mint_authority=None, freeze_authority=None)
        return None  # metadata missing = OK

    monkeypatch.setattr(safety.rpc, "get_account_info", counting)
    monkeypatch.setattr(
        holders,
        "check_holder_concentration",
        lambda mint, **kw: holders.HolderResult(
            ok=True, reasons=[], checks={}, whale_snapshot={}
        ),
    )
    safety.check_token_safety("MINT", pool="POOL", dex="pumpswap")
    n1 = calls["n"]
    safety.check_token_safety("MINT", pool="POOL", dex="pumpswap")
    assert calls["n"] == n1, "第二次应命中缓存，不再打 RPC"


TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


def test_block_token2022_transfer_fee(monkeypatch):
    mint_val = _mint_value(mint_authority=None, freeze_authority=None, owner=TOKEN_2022)
    mint_val["data"]["parsed"]["info"]["extensions"] = [
        {"extension": "transferFeeConfig", "state": {"newerTransferFee": {"transferFeeBasisPoints": 500}}}
    ]
    _patch(monkeypatch, mint_val, PUMPSWAP_PROGRAM)
    r = safety.check_token_safety("MINT", pool="POOL", dex="pumpswap")
    assert not r.ok
    assert any("transferFee" in x or "转账税" in x for x in r.reasons)


def test_block_token2022_permanent_delegate(monkeypatch):
    mint_val = _mint_value(mint_authority=None, freeze_authority=None, owner=TOKEN_2022)
    mint_val["data"]["parsed"]["info"]["extensions"] = [
        {"extension": "permanentDelegate", "state": {"delegate": AUTH}}
    ]
    _patch(monkeypatch, mint_val, PUMPSWAP_PROGRAM)
    r = safety.check_token_safety("MINT", pool="POOL", dex="pumpswap")
    assert not r.ok
    assert any("permanentDelegate" in x or "永久代理" in x for x in r.reasons)


def test_block_unrevoked_update_authority(monkeypatch):
    from solders.pubkey import Pubkey

    mint_val = _mint_value(mint_authority=None, freeze_authority=None)
    # 构造假 metadata：key + update_auth + mint
    ua = bytes(Pubkey.from_string(AUTH))
    meta_raw = bytes([4]) + ua + b"\x00" * 32

    def fake(pubkey, *, encoding="base64", commitment="confirmed"):
        if pubkey == "POOL":
            return {"owner": PUMPSWAP_PROGRAM, "data": ["", "base64"]}
        if pubkey == GOOD_MINT:
            return mint_val
        return {
            "owner": "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s",
            "data": [base64.b64encode(meta_raw).decode(), "base64"],
        }

    monkeypatch.setattr(safety.rpc, "get_account_info", fake)
    monkeypatch.setattr(
        holders,
        "check_holder_concentration",
        lambda mint, **kw: holders.HolderResult(
            ok=True, reasons=[], checks={}, whale_snapshot={}
        ),
    )
    r = safety.check_token_safety(GOOD_MINT, pool="POOL", dex="pumpswap")
    assert not r.ok
    assert any("updateAuthority" in x or "诱饵" in x for x in r.reasons)


def test_block_blacklisted_creator(monkeypatch):
    from pumpfun import blacklist

    creator = AUTH
    monkeypatch.setattr(blacklist, "known_bad_wallets", lambda: {creator})
    # PumpSwap 池账户带 creator @11
    raw = bytearray(43)
    from solders.pubkey import Pubkey

    raw[11:43] = bytes(Pubkey.from_string(creator))
    pool_acc = {
        "owner": PUMPSWAP_PROGRAM,
        "data": [base64.b64encode(bytes(raw)).decode(), "base64"],
    }
    mint_val = _mint_value(mint_authority=None, freeze_authority=None)

    def fake(pubkey, *, encoding="base64", commitment="confirmed"):
        if pubkey == "POOL":
            return pool_acc
        if pubkey == "MINT":
            return mint_val
        return None

    monkeypatch.setattr(safety.rpc, "get_account_info", fake)
    monkeypatch.setattr(
        holders,
        "check_holder_concentration",
        lambda mint, **kw: holders.HolderResult(
            ok=True, reasons=[], checks={}, whale_snapshot={}
        ),
    )
    r = safety.check_token_safety("MINT", pool="POOL", dex="pumpswap")
    assert not r.ok
    assert any("黑名单" in x for x in r.reasons)
