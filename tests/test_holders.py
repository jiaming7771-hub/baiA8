"""筹码集中度 / 老鼠仓防御单元测试。"""

from __future__ import annotations

import pytest

from pumpfun import config as C
from pumpfun import holders
from pumpfun import safety
from pumpfun.onchain_price import PUMPSWAP_PROGRAM


@pytest.fixture(autouse=True)
def _clear():
    holders.clear_cache()
    safety.clear_cache()
    yield
    holders.clear_cache()
    safety.clear_cache()


def _largest(rows):
    """rows: [(address, amount_raw), ...]"""
    return [
        {"address": a, "amount_raw": amt, "decimals": 6, "ui_amount": amt / 1e6}
        for a, amt in rows
    ]


def test_block_when_top10_over_threshold(monkeypatch):
    supply = 1_000_000_000_000  # 1e12
    # 流动性 vault 占 50%；前10非流动性合计 45% → 超 40% 拦截
    vault = "Vault1111111111111111111111111111111111111"
    whales = [(f"Whale{i:02d}{'x'*32}", int(supply * 0.045)) for i in range(10)]
    monkeypatch.setattr(
        holders.rpc, "get_mint_supply_raw", lambda m: (supply, 6)
    )
    monkeypatch.setattr(
        holders.rpc,
        "get_token_largest_accounts",
        lambda m: _largest([(vault, supply // 2)] + whales),
    )
    monkeypatch.setattr(
        holders, "_liquidity_token_accounts", lambda mint, pool: {vault}
    )

    r = holders.check_holder_concentration("MINT", pool="POOL")
    assert not r.ok
    assert any("筹码过度集中" in x for x in r.reasons)
    assert r.checks["top_pct"] == pytest.approx(0.45, abs=0.01)


def test_pass_when_top10_under_threshold(monkeypatch):
    supply = 1_000_000_000_000
    vault = "Vault1111111111111111111111111111111111111"
    # 前10各占 2% → 合计 20% < 40%
    whales = [(f"Whale{i:02d}{'x'*32}", int(supply * 0.02)) for i in range(10)]
    monkeypatch.setattr(
        holders.rpc, "get_mint_supply_raw", lambda m: (supply, 6)
    )
    monkeypatch.setattr(
        holders.rpc,
        "get_token_largest_accounts",
        lambda m: _largest([(vault, int(supply * 0.6))] + whales),
    )
    monkeypatch.setattr(
        holders, "_liquidity_token_accounts", lambda mint, pool: {vault}
    )

    r = holders.check_holder_concentration("MINT", pool="POOL")
    assert r.ok, r.reasons
    assert len(r.whale_snapshot) == 10


def test_fail_closed_on_rpc_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("RPC timeout")

    monkeypatch.setattr(holders.rpc, "get_mint_supply_raw", boom)
    r = holders.check_holder_concentration("MINT", pool="POOL")
    assert not r.ok
    assert any("RPC" in x or "超时" in x for x in r.reasons)


def test_early_whale_dump_triggers(monkeypatch):
    snap = {"W1" + "x" * 40: 1_000_000, "W2" + "x" * 40: 1_000_000}
    # 两个大户各卖掉 30% → 合计净流出 30% ≥ 20%
    monkeypatch.setattr(
        holders.rpc,
        "get_token_largest_accounts",
        lambda m: _largest(
            [(a, int(amt * 0.7)) for a, amt in snap.items()]
        ),
    )
    dump, meta = holders.detect_early_whale_dump("MINT", snapshot=snap)
    assert dump is True
    assert meta["dump_pct"] == pytest.approx(0.30, abs=0.01)


def test_early_whale_no_false_alarm(monkeypatch):
    snap = {"W1" + "x" * 40: 1_000_000, "W2" + "x" * 40: 1_000_000}
    monkeypatch.setattr(
        holders.rpc,
        "get_token_largest_accounts",
        lambda m: _largest([(a, amt) for a, amt in snap.items()]),
    )
    dump, meta = holders.detect_early_whale_dump("MINT", snapshot=snap)
    assert dump is False
    assert meta["dump_pct"] == pytest.approx(0.0)


def test_safety_includes_holder_block(monkeypatch):
    """集成：safety.check_token_safety 应并入集中度失败原因。"""
    monkeypatch.setattr(C, "SAFETY_CHECK_ENABLED", True)

    # 权限通过
    mint_val = {
        "owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        "data": {
            "parsed": {
                "type": "mint",
                "info": {"mintAuthority": None, "freezeAuthority": None},
            }
        },
    }

    def fake_get_account_info(pubkey, *, encoding="base64", commitment="confirmed"):
        if pubkey == "POOL":
            return {"owner": PUMPSWAP_PROGRAM, "data": ["", "base64"]}
        return mint_val

    monkeypatch.setattr(safety.rpc, "get_account_info", fake_get_account_info)
    monkeypatch.setattr(
        holders,
        "check_holder_concentration",
        lambda mint, **kw: holders.HolderResult(
            ok=False,
            reasons=["筹码过度集中/老鼠仓控盘 (前10大非流动性持仓占供应量 55.0% > 40%)"],
            checks={"top_pct": 0.55},
            whale_snapshot={},
        ),
    )

    r = safety.check_token_safety("MINT", pool="POOL", dex="pumpswap", use_cache=False)
    assert not r.ok
    assert any("筹码过度集中" in x for x in r.reasons)


def test_config_defaults():
    assert C.HOLDER_TOP10_MAX_PCT == pytest.approx(0.40)
    assert C.EARLY_WHALE_WINDOW_SEC == pytest.approx(120)
    assert C.EARLY_WHALE_DUMP_PCT == pytest.approx(0.20)
    assert C.BUNDLE_CHECK_ENABLED is True
    assert C.BUNDLE_MAX_PCT == pytest.approx(0.35)


def _owner_value(owner):
    return {"data": {"parsed": {"type": "account", "info": {"owner": owner}}}}


def test_owner_aggregation_merges_multiple_atas(monkeypatch):
    """同一控制人开多个 ATA 分仓，聚合后应按 owner 计入集中度。"""
    supply = 1_000_000_000_000
    vault = "Vault1111111111111111111111111111111111111"
    # 一个庄家用 10 个 ATA 各持 4.5%（账户级每个都不显眼），owner 都相同
    boss = "Boss" + "z" * 40
    atas = [(f"Ata{i:02d}{'x'*32}", int(supply * 0.045)) for i in range(10)]
    monkeypatch.setattr(holders.rpc, "get_mint_supply_raw", lambda m: (supply, 6))
    monkeypatch.setattr(
        holders.rpc,
        "get_token_largest_accounts",
        lambda m: _largest([(vault, supply // 2)] + atas),
    )
    monkeypatch.setattr(holders, "_liquidity_token_accounts", lambda mint, pool: {vault})
    # 所有 ATA 都归属同一 owner
    monkeypatch.setattr(
        holders.rpc,
        "get_multiple_accounts",
        lambda keys, **kw: [_owner_value(boss) for _ in keys],
    )
    # 避免真实网络：funder 无法解析
    monkeypatch.setattr(holders, "_find_funder", lambda w: None)

    r = holders.check_holder_concentration("MINT", pool="POOL")
    assert not r.ok
    # 聚合后单一 owner 占 45% → 超阈值
    assert r.checks["distinct_owners"] == 1
    assert r.checks["top_pct"] == pytest.approx(0.45, abs=0.01)


def test_bundle_same_funder_blocks(monkeypatch):
    """多个独立小号（各自 owner 不同）但同一资金源喂出 → 捆绑拦截。"""
    supply = 1_000_000_000_000
    vault = "Vault1111111111111111111111111111111111111"
    # 6 个钱包各持 6% = 36% 总量（未超 40% 集中度、流通盘 60% 也未超），但同源 → 捆绑
    wallets = [(f"W{i:02d}{'x'*38}", int(supply * 0.06)) for i in range(6)]
    monkeypatch.setattr(holders.rpc, "get_mint_supply_raw", lambda m: (supply, 6))
    monkeypatch.setattr(
        holders.rpc,
        "get_token_largest_accounts",
        lambda m: _largest([(vault, int(supply * 0.4))] + wallets),
    )
    monkeypatch.setattr(holders, "_liquidity_token_accounts", lambda mint, pool: {vault})
    # 每个 token 账户 owner = 其自身地址（各不相同）
    monkeypatch.setattr(
        holders.rpc,
        "get_multiple_accounts",
        lambda keys, **kw: [_owner_value(k) for k in keys],
    )
    # 全部同一 funder
    monkeypatch.setattr(holders, "_find_funder", lambda w: "MotherWallet" + "m" * 32)

    r = holders.check_holder_concentration("MINT", pool="POOL")
    assert not r.ok
    assert any("捆绑" in x or "多钱包" in x for x in r.reasons)
    assert r.checks["bundle"]["cluster_wallets"] >= 2


def test_bundle_distinct_funders_pass(monkeypatch):
    """各钱包资金源互不相同 → 非捆绑，放行。"""
    supply = 1_000_000_000_000
    vault = "Vault1111111111111111111111111111111111111"
    wallets = [(f"W{i:02d}{'x'*38}", int(supply * 0.03)) for i in range(6)]
    monkeypatch.setattr(holders.rpc, "get_mint_supply_raw", lambda m: (supply, 6))
    monkeypatch.setattr(
        holders.rpc,
        "get_token_largest_accounts",
        lambda m: _largest([(vault, int(supply * 0.7))] + wallets),
    )
    monkeypatch.setattr(holders, "_liquidity_token_accounts", lambda mint, pool: {vault})
    monkeypatch.setattr(
        holders.rpc,
        "get_multiple_accounts",
        lambda keys, **kw: [_owner_value(k) for k in keys],
    )
    # 每个钱包独立资金源
    monkeypatch.setattr(holders, "_find_funder", lambda w: "F" + w)

    r = holders.check_holder_concentration("MINT", pool="POOL")
    assert r.ok, r.reasons
    assert r.checks["bundle"]["blocked"] is False


def test_find_funder_picks_largest_sender(monkeypatch):
    wallet = "Wallet" + "w" * 38
    mother = "Mother" + "m" * 38
    monkeypatch.setattr(
        holders.rpc,
        "get_signatures_for_address",
        lambda addr, **kw: [{"signature": "sigNew"}, {"signature": "sigOld"}],
    )
    monkeypatch.setattr(
        holders.rpc,
        "get_transaction_meta",
        lambda sig: {
            "account_keys": [mother, wallet],
            "pre_balances": [1_000_000_000, 0],
            "post_balances": [900_000_000, 100_000_000],
        },
    )
    assert holders._find_funder(wallet) == mother
