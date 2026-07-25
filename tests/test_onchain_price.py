"""链上报价解码单测（不依赖真实 RPC）。"""

from __future__ import annotations

import base64
import struct

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
