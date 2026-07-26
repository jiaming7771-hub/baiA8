"""链上报价解码单测（不依赖真实 RPC）。"""

from __future__ import annotations

import base64
import struct

import pytest
from solders.pubkey import Pubkey

from pumpfun import onchain_price as op


def _acct(raw: bytes, owner: str) -> dict:
    return {
        "owner": owner,
        "lamports": 1,
        "data": [base64.b64encode(raw).decode("ascii"), "base64"],
        "executable": False,
    }


def _tok(amount: int) -> dict:
    buf = bytearray(165)
    struct.pack_into("<Q", buf, 64, amount)
    return _acct(bytes(buf), op.TOKEN_PROGRAM)


def _pool(
    *,
    base_mint: bytes,
    quote_mint: bytes,
    base_vault: bytes,
    quote_vault: bytes,
    quote_virtual: int = 0,
    size: int = 301,
) -> bytes:
    """真实 PumpSwap Pool 账户布局（301 字节）。"""
    raw = bytearray(size)
    raw[43:75] = base_mint
    raw[75:107] = quote_mint
    raw[139:171] = base_vault
    raw[171:203] = quote_vault
    if size >= 253:
        struct.pack_into("<Q", raw, 245, quote_virtual)
    return bytes(raw)


def test_bonding_curve_price_decode():
    # virtual_token=1_000_000_000 (1000 tokens @6dec), virtual_sol=1_000_000_000 (1 SOL)
    raw = bytearray(82)
    struct.pack_into("<Q", raw, 8, 1_000_000_000)  # virtual_token
    struct.pack_into("<Q", raw, 16, 1_000_000_000)  # virtual_sol
    px = op.price_from_bonding_curve_account(_acct(bytes(raw), op.PUMP_PROGRAM))
    # (1e9/1e9) / (1e9/1e6) = 1 / 1000 = 0.001
    assert px == 0.001


def test_pumpswap_price_quote_is_token():
    """base=WSOL, quote=token → price = sol/token."""
    raw = bytearray(220)
    base_mint = bytes(Pubkey.from_string(op.WSOL_MINT))
    quote_mint = bytes(bytearray([3] * 32))
    base_vault = bytes(bytearray([8] * 32))
    quote_vault = bytes(bytearray([7] * 32))
    raw[43:75] = base_mint
    raw[75:107] = quote_mint
    raw[139:171] = base_vault
    raw[171:203] = quote_vault

    # 2 SOL in base vault, 1_000_000 tokens (1 token @6dec) in quote → 2 SOL/token
    vaults = {
        str(Pubkey.from_bytes(base_vault)): _tok(2_000_000_000),
        str(Pubkey.from_bytes(quote_vault)): _tok(1_000_000),
    }
    px, meta = op.price_from_pumpswap_pool(
        _acct(bytes(raw), op.PUMPSWAP_PROGRAM),
        vault_accounts=vaults,
        token_decimals=6,
    )
    assert meta["base_mint"] == op.WSOL_MINT
    assert px == 2.0


def test_pumpswap_price_base_is_token():
    raw = bytearray(220)
    base_mint = bytes(bytearray([3] * 32))
    quote_mint = bytes(Pubkey.from_string(op.WSOL_MINT))
    base_vault = bytes(bytearray([4] * 32))
    quote_vault = bytes(bytearray([5] * 32))
    raw[43:75] = base_mint
    raw[75:107] = quote_mint
    raw[139:171] = base_vault
    raw[171:203] = quote_vault

    # 500_000 tokens (0.5 @6dec), 1 SOL → 2 SOL/token
    vaults = {
        str(Pubkey.from_bytes(base_vault)): _tok(500_000),
        str(Pubkey.from_bytes(quote_vault)): _tok(1_000_000_000),
    }
    px, _ = op.price_from_pumpswap_pool(
        _acct(bytes(raw), op.PUMPSWAP_PROGRAM),
        vault_accounts=vaults,
        token_decimals=6,
    )
    assert px == 2.0


# ---------------------------------------------------------------- 虚拟储备
# 核心正确性：PumpSwap 的兑换曲线用 quote_vault + 虚拟储备(u64@245)，
# 漏掉它 → 系统性低报，且池子越薄倍数越大（低报倍数 = 1 + 虚拟/真实）。
# 迁移池实测虚拟储备恒 ≈17.5845 SOL（2026-07-27 实测 9 个池全部同值）。
_VIRT_MIGRATED = 17_584_505_288  # lamports
_BASE_M = bytes(bytearray([3] * 32))
_QUOTE_WSOL = bytes(Pubkey.from_string(op.WSOL_MINT))
_BV = bytes(bytearray([4] * 32))
_QV = bytes(bytearray([5] * 32))


def _price_with(*, sol_lamports: int, tokens_raw: int, virtual: int) -> float | None:
    raw = _pool(
        base_mint=_BASE_M,
        quote_mint=_QUOTE_WSOL,
        base_vault=_BV,
        quote_vault=_QV,
        quote_virtual=virtual,
    )
    vaults = {
        str(Pubkey.from_bytes(_BV)): _tok(tokens_raw),
        str(Pubkey.from_bytes(_QV)): _tok(sol_lamports),
    }
    px, _ = op.price_from_pumpswap_pool(
        _acct(raw, op.PUMPSWAP_PROGRAM), vault_accounts=vaults, token_decimals=6
    )
    return px


def test_quote_virtual_reserve_is_decoded():
    raw = _pool(
        base_mint=_BASE_M,
        quote_mint=_QUOTE_WSOL,
        base_vault=_BV,
        quote_vault=_QV,
        quote_virtual=_VIRT_MIGRATED,
    )
    acc = _acct(raw, op.PUMPSWAP_PROGRAM)
    assert op.pumpswap_quote_virtual_reserve(acc) == _VIRT_MIGRATED
    # 短账户（旧布局 / 测试桩）读不到该字段时必须退化为 0，不能抛
    short = _acct(bytes(bytearray(220)), op.PUMPSWAP_PROGRAM)
    assert op.pumpswap_quote_virtual_reserve(short) == 0


def test_price_includes_quote_virtual_reserve():
    """10 SOL 真实 + 10 SOL 虚拟 + 1000 token → 0.02，而非漏算的 0.01。"""
    px = _price_with(
        sol_lamports=10_000_000_000, tokens_raw=1_000_000_000, virtual=10_000_000_000
    )
    assert px == 0.02


def test_underreport_factor_grows_as_pool_thins():
    """低报倍数 = 1 + 虚拟/真实：薄池被低报得远比厚池严重（根因的判据）。"""
    tokens = 1_000_000_000_000  # 1e6 token @6dec
    factors = []
    for sol in (10, 50, 200, 3600):
        lam = sol * 1_000_000_000
        naive = _price_with(sol_lamports=lam, tokens_raw=tokens, virtual=0)
        fixed = _price_with(sol_lamports=lam, tokens_raw=tokens, virtual=_VIRT_MIGRATED)
        factors.append(fixed / naive)
        # 解析式必须成立
        assert fixed / naive == pytest.approx(
            1.0 + (_VIRT_MIGRATED / 1e9) / sol, rel=1e-9
        )
    # 单调递减：越厚的池偏差越小
    assert factors == sorted(factors, reverse=True)
    assert factors[0] > 2.7      # 10 SOL 池被低报 2.7 倍以上
    assert factors[-1] < 1.01    # 3600 SOL 池偏差 <1%


def test_virtual_reserve_applies_to_quote_side_when_wsol_is_base():
    """WSOL 在 base 侧时虚拟储备记在 quote(=token) 侧，绝不能加到 SOL 上。"""
    raw = _pool(
        base_mint=_QUOTE_WSOL,
        quote_mint=_BASE_M,
        base_vault=_BV,
        quote_vault=_QV,
        quote_virtual=1_000_000,  # 1 token @6dec
    )
    vaults = {
        str(Pubkey.from_bytes(_BV)): _tok(2_000_000_000),  # 2 SOL
        str(Pubkey.from_bytes(_QV)): _tok(1_000_000),      # 1 token
    }
    px, meta = op.price_from_pumpswap_pool(
        _acct(raw, op.PUMPSWAP_PROGRAM), vault_accounts=vaults, token_decimals=6
    )
    # 2 SOL / (1 + 1) token = 1.0（若错加到 SOL 侧会得到 3.0）
    assert px == 1.0
    assert meta["sol_vault"] == 2.0


def test_sol_vault_excludes_virtual_reserve():
    """抽池判定只能看真金白银：sol_vault 不含虚拟储备。"""
    raw = _pool(
        base_mint=_BASE_M,
        quote_mint=_QUOTE_WSOL,
        base_vault=_BV,
        quote_vault=_QV,
        quote_virtual=_VIRT_MIGRATED,
    )
    vaults = {
        str(Pubkey.from_bytes(_BV)): _tok(1_000_000_000),
        str(Pubkey.from_bytes(_QV)): _tok(3_000_000_000),  # 3 SOL 真实
    }
    _, meta = op.price_from_pumpswap_pool(
        _acct(raw, op.PUMPSWAP_PROGRAM), vault_accounts=vaults, token_decimals=6
    )
    assert meta["sol_vault"] == 3.0
    assert meta["quote_virtual_raw"] == _VIRT_MIGRATED


def test_drained_pool_returns_sentinel_not_virtual_reserve_price():
    """金库被抽干时必须给近零哨兵。

    加了虚拟储备后，残池（金库剩几个 lamport）会被虚拟的 17.58 SOL 撑出一个
    非零假价——实测 StableGuy 金库 2 lamports 仍报 2.57e-07，真实可成交价
    1.17e-03，差 4500 倍。此测试锁死这条回归。
    """
    px = _price_with(
        sol_lamports=2, tokens_raw=7_361, virtual=_VIRT_MIGRATED
    )
    assert px == 1e-18

    raw = _pool(
        base_mint=_BASE_M,
        quote_mint=_QUOTE_WSOL,
        base_vault=_BV,
        quote_vault=_QV,
        quote_virtual=_VIRT_MIGRATED,
    )
    vaults = {
        str(Pubkey.from_bytes(_BV)): _tok(7_361),
        str(Pubkey.from_bytes(_QV)): _tok(2),
    }
    _, meta = op.price_from_pumpswap_pool(
        _acct(raw, op.PUMPSWAP_PROGRAM), vault_accounts=vaults, token_decimals=6
    )
    assert meta["vault_drained"] is True


def test_pool_just_above_dead_threshold_still_prices():
    """刚好高于枯竭门槛的池子仍要正常报价（别把活池误判成死池）。"""
    px = _price_with(
        sol_lamports=60_000_000,  # 0.06 SOL > 0.05 门槛
        tokens_raw=1_000_000_000,
        virtual=0,
    )
    assert px == 0.06 / 1000.0
