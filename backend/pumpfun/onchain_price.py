"""持仓秒级链上报价：直接读 Pump.fun BondingCurve / PumpSwap / Meteora DBC 池账户。

废弃 DexScreener 持仓喂价。价格来源：
1) pool 账户 owner = pump bonding-curve → 解码 virtual reserves
2) pool 账户 owner = PumpSwap AMM → 读两侧 vault SPL token amount 比值
3) pool 账户 owner = Meteora DBC → 解码 sqrt_price（Q64.64）
4) 必要时由 mint 推导 bonding-curve PDA

读不出价必须让上层看见：任何失败路径都带 reason 回传（见
`fetch_prices_for_positions` 的 mark_stale_since），绝不能静默跳过让持仓
沿用不动的旧 mark。
"""

from __future__ import annotations

import base64
import logging
import struct
import time
from typing import Any

from solders.pubkey import Pubkey

from . import config as C
from . import rpc

logger = logging.getLogger("pumpfun.onchain_price")

WSOL_MINT = "So11111111111111111111111111111111111111112"
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMPSWAP_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
METEORA_DBC_PROGRAM = "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

# PumpSwap Pool（8 字节 discriminator 之后）
# bump u8 + index u16 + creator/base/quote/lp/base_vault/quote_vault pubkeys
# ... lp_supply u64 @203 + coin_creator pubkey @211 + 2 字节 flags @243
_OFF_BASE_MINT = 43
_OFF_QUOTE_MINT = 75
_OFF_LP_MINT = 107
_OFF_BASE_VAULT = 139
_OFF_QUOTE_VAULT = 171
_OFF_LP_SUPPLY = 203

# ★ quote 侧「虚拟储备」u64（quote mint 的最小单位）。
#
# PumpSwap 的兑换曲线用的不是金库余额本身，而是 quote_vault_amount + 这个字段。
# pump.fun 毕业迁池时只把募到的 ~67.4 SOL 真金白银打进 quote 金库，另外
# ~17.5845 SOL 只作为虚拟储备记在池账户里（lp_supply 也是按含虚拟储备的
# 全额算的：sqrt(206.9e6 * 1e6 * 85e9) ≈ 4.1934e12 = 实测 lp_supply）。
# 漏掉它 → 分母口径没错、分子少了一截 → 价格系统性低报，且低报倍数
# = 1 + 17.5845 / quote_vault_sol，池子越薄倍数越大。
#
# 实测（2026-07-27 02:22 三方对照，见报告）：
#   BUB   金库   9.35 SOL → 低报 2.92 倍
#   FI$H  金库  53.18 SOL → 低报 1.35 倍
#   TBB   金库 3634.5 SOL → 低报 1.008 倍
# 非迁移池（手动 create_pool）与已死池该字段为 0，加了也不会动它们的价格。
_OFF_QUOTE_VIRTUAL_RESERVE = 245

# 读到该字段所需的最小账户长度（245 + 8）
_POOL_LEN_WITH_VIRTUAL = 253

# ---------------------------------------------------------------- Meteora DBC
# VirtualPool 账户（424 字节，discriminator d5e005d16245775c）：
#   8   volatility_tracker（u64 ts + 8 pad + 3×u128 = 64）
#   72  config / 104 creator / 136 base_mint / 168 base_vault / 200 quote_vault
#   232 base_reserve u64 / 240 quote_reserve u64 / 248..280 四项手续费 u64
#   280 sqrt_price u128（Q64.64，quote 最小单位 per base 最小单位）
#   296 activation_point u64 / 304 pool_type u8 / 305 is_migrated u8
# 2026-07-27 实测校验：offset 136 解出的 base_mint 与 DexScreener 给的 mint 一致，
# 价格与 DexScreener priceNative 在未迁移池上吻合（CATE/CATECOIN/CHUNGUS/perv
# 六次采样比值 0.85~1.06）。
_DBC_OFF_BASE_MINT = 136
_DBC_OFF_BASE_VAULT = 168
_DBC_OFF_QUOTE_VAULT = 200
_DBC_OFF_SQRT_PRICE = 280
_DBC_OFF_IS_MIGRATED = 305
_DBC_MIN_LEN = _DBC_OFF_IS_MIGRATED + 1

# ★ 迁移标志一旦置位，DBC 账户的 sqrt_price 就永久冻结（交易搬去 DAMM 池）。
# 实测 CHUNGUS / perv 迁移后冻结价比真实盘口低 24%~28%，而金库分别只剩
# 0.167 / 0.036 SOL——即「读得到数字但数字是死的」。这种池必须报「读不到」
# 而不是报价，也绝不能当成抽干：币在别的池子还活着，按 0 记就是假的 100% 亏损。
_DBC_Q64 = float(1 << 64)

# 默认 pump token decimals；mint 账户读失败时回退
_DEFAULT_TOKEN_DECIMALS = 6
_WSOL_DECIMALS = 9

# quote 金库真实 SOL 低于此值即视为被抽干：此时曲线只剩虚拟储备，
# 比值算出来的价是纯幻觉（实测 StableGuy 金库 2 lamports 仍报 2.57e-07，
# 真实可成交价 1.17e-03，差 4500 倍）。
_DEAD_POOL_SOL = 0.05

_price_cache: dict[str, dict[str, Any]] = {}  # mint -> {price, source, ts, pool}


def _b64_data(account: dict[str, Any] | None) -> bytes | None:
    if not account:
        return None
    data = account.get("data")
    if not isinstance(data, list) or not data:
        return None
    try:
        return base64.b64decode(data[0])
    except Exception:
        return None


def _pk_at(raw: bytes, off: int) -> str:
    return str(Pubkey.from_bytes(raw[off : off + 32]))


def _spl_amount(account: dict[str, Any] | None) -> int | None:
    raw = _b64_data(account)
    if raw is None or len(raw) < 72:
        return None
    return int(struct.unpack_from("<Q", raw, 64)[0])


def _spl_mint(account: dict[str, Any] | None) -> str | None:
    """SPL Token 账户所属 mint（offset 0）。"""
    raw = _b64_data(account)
    if raw is None or len(raw) < 32:
        return None
    return _pk_at(raw, 0)


def _mint_decimals(account: dict[str, Any] | None) -> int | None:
    """SPL Mint：decimals 在 offset 44（u8）。"""
    raw = _b64_data(account)
    if raw is None or len(raw) < 45:
        return None
    return int(raw[44])


def price_from_bonding_curve_account(account: dict[str, Any]) -> float | None:
    """BondingCurve: virtual_token/virtual_sol (u64 @8/@16)，token 6 / SOL 9。"""
    raw = _b64_data(account)
    if raw is None or len(raw) < 48:
        return None
    virtual_token, virtual_sol = struct.unpack_from("<2Q", raw, 8)
    if virtual_token <= 0 or virtual_sol <= 0:
        return None
    # SOL per token
    return (virtual_sol / 1e9) / (virtual_token / 1e6)


def bonding_curve_progress_pct(account: dict[str, Any]) -> float | None:
    """曲线进度 0~100：real_sol_reserves / 毕业 SOL；complete=True → 100。

    账户布局：@24 real_token, @32 real_sol, @48 complete。
    """
    raw = _b64_data(account)
    if raw is None or len(raw) < 49:
        return None
    real_sol = struct.unpack_from("<Q", raw, 32)[0] / 1e9
    complete = bool(raw[48])
    if complete:
        return 100.0
    grad = max(1.0, float(C.BONDING_GRADUATION_SOL))
    return round(min(100.0, max(0.0, real_sol / grad * 100.0)), 2)


def fetch_bonding_progress_pct(
    mint: str,
    *,
    pool: str | None = None,
    dex: str | None = None,
) -> tuple[float | None, str]:
    """返回 (进度%, 来源说明)。pumpswap / 已毕业 → 100；读不到或认不出 → (None, reason)。

    「认不出这个池子的程序」必须报 None（unknown_owner:…），不能报 100：
    100 的含义是「已毕业」，是准入侧最严的一档。把未知说成已毕业，等于让一个
    从没被检查过的场所顶着满分穿过 graduated-only 闸门。未知不等于安全。
    """
    dex_l = (dex or "").lower()
    if dex_l in ("pumpswap",):
        return 100.0, "pumpswap"
    pool_addr = resolve_pool_for_mint(mint, pool)
    if not pool_addr:
        try:
            pool_addr = bonding_curve_pda(mint)
        except Exception as exc:
            return None, f"no_pool:{exc}"
    try:
        acc = rpc.get_account_info(pool_addr)
    except rpc.RpcError as exc:
        return None, f"rpc:{exc}"
    if not acc:
        return None, "empty_account"
    owner = str(acc.get("owner") or "")
    if owner == PUMPSWAP_PROGRAM:
        return 100.0, "pumpswap_owner"
    if owner != PUMP_PROGRAM:
        # 既不是曲线程序也不是 PumpSwap：毕业状态**未测**，与「已毕业」严格区分
        return None, f"unknown_owner:{owner[:8]}"
    pct = bonding_curve_progress_pct(acc)
    if pct is None:
        return None, "decode_fail"
    return pct, "bonding_curve"


def pumpswap_quote_virtual_reserve(pool_account: dict[str, Any]) -> int:
    """池账户里 quote 侧虚拟储备（quote mint 最小单位）。读不到 → 0。

    见 `_OFF_QUOTE_VIRTUAL_RESERVE` 的注释：这是 PumpSwap 定价必须算进去的一项。
    """
    raw = _b64_data(pool_account)
    if raw is None or len(raw) < _POOL_LEN_WITH_VIRTUAL:
        return 0
    try:
        return int(struct.unpack_from("<Q", raw, _OFF_QUOTE_VIRTUAL_RESERVE)[0])
    except Exception:
        return 0


def price_from_pumpswap_pool(
    pool_account: dict[str, Any],
    *,
    vault_accounts: dict[str, dict[str, Any] | None] | None = None,
    token_decimals: int = _DEFAULT_TOKEN_DECIMALS,
) -> tuple[float | None, dict[str, Any]]:
    """返回 (price_sol, meta)。

    价格口径 = (quote 金库余额 + quote 虚拟储备) / base 金库余额，与 PumpSwap
    程序自己的兑换曲线一致（漏掉虚拟储备就是系统性低报的根因）。

    meta 恒含 base/quote mint+vault，以及 sol_vault（WSOL 侧**真实可提**数量，
    不含虚拟储备——抽池判定必须只看真金白银）。
    抽池/砸干后 quote_amt≈0 时旧逻辑直接 return None → 持仓继续用过期 mark
    （CXMT +23% 假价根因）。现在即使 SOL 侧枯竭也回传 sol_vault=0，由上层逃生。
    """
    raw = _b64_data(pool_account)
    meta: dict[str, Any] = {}
    if raw is None or len(raw) < 203:
        return None, meta
    base_mint = _pk_at(raw, _OFF_BASE_MINT)
    quote_mint = _pk_at(raw, _OFF_QUOTE_MINT)
    base_vault = _pk_at(raw, _OFF_BASE_VAULT)
    quote_vault = _pk_at(raw, _OFF_QUOTE_VAULT)
    quote_virtual = pumpswap_quote_virtual_reserve(pool_account)
    meta = {
        "base_mint": base_mint,
        "quote_mint": quote_mint,
        "base_vault": base_vault,
        "quote_vault": quote_vault,
        "quote_virtual_raw": quote_virtual,
    }
    vaults = vault_accounts or {}
    base_amt = _spl_amount(vaults.get(base_vault))
    quote_amt = _spl_amount(vaults.get(quote_vault))
    # WSOL 侧绝对深度：砸盘抽干时第一信号（比价格比值更早、更稳）。
    # 只记真实金库余额：虚拟储备提不出来，算进去会把抽干的池子看成还有底。
    if base_mint == WSOL_MINT and base_amt is not None:
        meta["sol_vault"] = float(base_amt) / 1e9
    elif quote_mint == WSOL_MINT and quote_amt is not None:
        meta["sol_vault"] = float(quote_amt) / 1e9
    else:
        meta["sol_vault"] = None

    sol_vault = meta.get("sol_vault")
    dead_sol = sol_vault is not None and float(sol_vault) <= _DEAD_POOL_SOL
    if base_amt is None or quote_amt is None or base_amt <= 0 or quote_amt <= 0 or dead_sol:
        # SOL 侧被砸干：给一个近零价，逼硬止损/逃生，绝不能让上层沿用旧 mark。
        # 注意必须在加虚拟储备之前判定：否则金库归零的池子会被虚拟储备
        # 撑出一个非零假价（本次修复引入的新风险，这里堵掉）。
        if dead_sol:
            meta["vault_drained"] = True
            return 1e-18, meta
        return None, meta
    dec = max(0, int(token_decimals))
    if base_mint == WSOL_MINT:
        # WSOL 在 base 侧：虚拟储备记在 quote（= token）侧
        price = (base_amt / 1e9) / ((quote_amt + quote_virtual) / (10**dec))
    elif quote_mint == WSOL_MINT:
        price = ((quote_amt + quote_virtual) / 1e9) / (base_amt / (10**dec))
    else:
        return None, meta
    return float(price), meta


def meteora_dbc_is_migrated(pool_account: dict[str, Any]) -> bool:
    raw = _b64_data(pool_account)
    if raw is None or len(raw) < _DBC_MIN_LEN:
        return False
    return bool(raw[_DBC_OFF_IS_MIGRATED])


def price_from_meteora_dbc_pool(
    pool_account: dict[str, Any],
    *,
    vault_accounts: dict[str, dict[str, Any] | None] | None = None,
    base_decimals: int = _DEFAULT_TOKEN_DECIMALS,
) -> tuple[float | None, dict[str, Any]]:
    """返回 (price_sol, meta)。价格 = (sqrt_price / 2^64)^2 换算到 UI 口径。

    meta 与 PumpSwap 路径同构：sol_vault 只记 quote 金库**真实可提** SOL，
    抽干（≤ _DEAD_POOL_SOL）时回近零哨兵逼逃生。
    已迁移池（is_migrated）回 (None, {dbc_migrated: True})——价被冻结，
    只能算「读不到」，不能算「价归零」。
    """
    raw = _b64_data(pool_account)
    meta: dict[str, Any] = {}
    if raw is None or len(raw) < _DBC_MIN_LEN:
        return None, meta
    base_mint = _pk_at(raw, _DBC_OFF_BASE_MINT)
    base_vault = _pk_at(raw, _DBC_OFF_BASE_VAULT)
    quote_vault = _pk_at(raw, _DBC_OFF_QUOTE_VAULT)
    meta = {
        "base_mint": base_mint,
        "base_vault": base_vault,
        "quote_vault": quote_vault,
        "sol_vault": None,
    }
    if raw[_DBC_OFF_IS_MIGRATED]:
        meta["dbc_migrated"] = True
        return None, meta

    vaults = vault_accounts or {}
    quote_acc = vaults.get(quote_vault)
    quote_mint = _spl_mint(quote_acc)
    meta["quote_mint"] = quote_mint
    if quote_mint is not None and quote_mint != WSOL_MINT:
        # 非 SOL 计价（USDC 等）：本机全部按 SOL 口径管仓，换算会引入汇率误差
        return None, meta
    quote_amt = _spl_amount(quote_acc)
    if quote_amt is not None:
        meta["sol_vault"] = float(quote_amt) / 1e9
        if float(meta["sol_vault"]) <= _DEAD_POOL_SOL:
            meta["vault_drained"] = True
            return 1e-18, meta

    sqrt_price = int.from_bytes(
        raw[_DBC_OFF_SQRT_PRICE : _DBC_OFF_SQRT_PRICE + 16], "little"
    )
    if sqrt_price <= 0:
        return None, meta
    dec = max(0, int(base_decimals))
    price = (sqrt_price / _DBC_Q64) ** 2 * (10**dec) / (10**_WSOL_DECIMALS)
    if price <= 0:
        return None, meta
    return float(price), meta


def bonding_curve_pda(mint: str) -> str:
    curve, _ = Pubkey.find_program_address(
        [b"bonding-curve", bytes(Pubkey.from_string(mint))],
        Pubkey.from_string(PUMP_PROGRAM),
    )
    return str(curve)


def resolve_pool_for_mint(mint: str, pool: str | None = None) -> str | None:
    """优先用传入/观察池 pool；否则尝试 bonding-curve PDA。"""
    if pool:
        return pool
    try:
        from . import market_data as md

        ent = (md._watchlist or {}).get(mint) or {}  # noqa: SLF001 — 同包内部复用
        if ent.get("pool"):
            return str(ent["pool"])
    except Exception:
        pass
    try:
        return bonding_curve_pda(mint)
    except Exception:
        return None


def fetch_pool_price_sol(
    mint: str,
    *,
    pool: str | None = None,
    dex: str | None = None,
) -> dict[str, Any] | None:
    """同步拉取链上价格。成功返回 {price, source, pool, owner, ts}。"""
    row, _ = fetch_pool_price_row(mint, pool=pool, dex=dex)
    return row


def fetch_pool_price_row(
    mint: str,
    *,
    pool: str | None = None,
    dex: str | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """同 `fetch_pool_price_sol`，但失败时同时回传原因串。

    原因要能区分「这条链路暂时抖动」（rpc:*）与「这个池我们根本读不懂」
    （unknown_owner:* / dbc_migrated），后者不会自愈，必须尽快逃生。
    """
    pool_addr = resolve_pool_for_mint(mint, pool)
    if not pool_addr:
        return None, "no_pool"
    try:
        pool_acc = rpc.get_account_info(pool_addr)
    except rpc.RpcError as exc:
        logger.warning("链上读池失败 mint=%s pool=%s: %s", mint[:8], pool_addr[:8], exc)
        return None, f"rpc:{exc}"
    if not pool_acc:
        # 池地址失效时回退 bonding-curve PDA
        alt = bonding_curve_pda(mint)
        if alt != pool_addr:
            try:
                pool_acc = rpc.get_account_info(alt)
                pool_addr = alt
            except rpc.RpcError:
                pool_acc = None
        if not pool_acc:
            return None, "empty_account"

    owner = str(pool_acc.get("owner") or "")
    price: float | None = None
    source = "unknown"
    fail_reason = "decode_fail"
    vault_meta: dict[str, Any] = {}

    if owner == PUMP_PROGRAM:
        price = price_from_bonding_curve_account(pool_acc)
        source = "pump_bonding_curve"
        # bonding curve 的 real_sol 可近似当「池内 SOL」；WSS 订池账户本身
        vault_meta["sol_vault_pubkey"] = pool_addr
        vault_meta["sol_vault_kind"] = "bonding"
        try:
            raw_bc = _b64_data(pool_acc) or b""
            if len(raw_bc) >= 40:
                vault_meta["sol_vault"] = struct.unpack_from("<Q", raw_bc, 32)[0] / 1e9
        except Exception:
            pass
    elif owner == PUMPSWAP_PROGRAM:
        raw = _b64_data(pool_acc) or b""
        if len(raw) < 203:
            return None, "short_account"
        base_vault = _pk_at(raw, _OFF_BASE_VAULT)
        quote_vault = _pk_at(raw, _OFF_QUOTE_VAULT)
        base_mint = _pk_at(raw, _OFF_BASE_MINT)
        quote_mint = _pk_at(raw, _OFF_QUOTE_MINT)
        token_mint = mint
        if mint not in (base_mint, quote_mint):
            token_mint = quote_mint if base_mint == WSOL_MINT else base_mint
        try:
            accounts = rpc.get_multiple_accounts([base_vault, quote_vault, token_mint])
        except rpc.RpcError as exc:
            logger.warning("读 vault 失败 %s: %s", mint[:8], exc)
            return None, f"rpc:{exc}"
        vault_map = {base_vault: accounts[0], quote_vault: accounts[1]}
        decimals = _mint_decimals(accounts[2]) or _DEFAULT_TOKEN_DECIMALS
        price, vault_meta = price_from_pumpswap_pool(
            pool_acc, vault_accounts=vault_map, token_decimals=decimals
        )
        if base_mint == WSOL_MINT:
            vault_meta["sol_vault_pubkey"] = base_vault
            vault_meta["sol_vault_kind"] = "spl"
        elif quote_mint == WSOL_MINT:
            vault_meta["sol_vault_pubkey"] = quote_vault
            vault_meta["sol_vault_kind"] = "spl"
        source = "pumpswap_vaults"
        if vault_meta.get("vault_drained"):
            source = "pumpswap_drained"
    elif owner == METEORA_DBC_PROGRAM:
        raw = _b64_data(pool_acc) or b""
        if len(raw) < _DBC_MIN_LEN:
            return None, "short_account"
        base_mint = _pk_at(raw, _DBC_OFF_BASE_MINT)
        base_vault = _pk_at(raw, _DBC_OFF_BASE_VAULT)
        quote_vault = _pk_at(raw, _DBC_OFF_QUOTE_VAULT)
        try:
            accounts = rpc.get_multiple_accounts([base_mint, base_vault, quote_vault])
        except rpc.RpcError as exc:
            logger.warning("读 DBC vault 失败 %s: %s", mint[:8], exc)
            return None, f"rpc:{exc}"
        vault_map = {base_vault: accounts[1], quote_vault: accounts[2]}
        decimals = _mint_decimals(accounts[0]) or _DEFAULT_TOKEN_DECIMALS
        price, vault_meta = price_from_meteora_dbc_pool(
            pool_acc, vault_accounts=vault_map, base_decimals=decimals
        )
        vault_meta["sol_vault_pubkey"] = quote_vault
        vault_meta["sol_vault_kind"] = "spl"
        source = "meteora_dbc"
        if vault_meta.get("dbc_migrated"):
            fail_reason = "dbc_migrated"
        elif vault_meta.get("vault_drained"):
            source = "meteora_dbc_drained"
    else:
        logger.warning(
            "未知池 owner=%s mint=%s pool=%s dex=%s", owner[:12], mint[:8], pool_addr[:8], dex
        )
        return None, f"unknown_owner:{owner[:8]}"

    # 抽干时 price 可能是近零哨兵；仍要回传让持仓管理能看到 sol_vault
    if (price is None or price <= 0) and not vault_meta.get("vault_drained"):
        return None, fail_reason

    row = {
        "mint": mint,
        "pool": pool_addr,
        "owner": owner,
        "price": float(price or 1e-18),
        "source": source,
        "dex": dex,
        "ts": time.time(),
        "sol_vault": vault_meta.get("sol_vault"),
        "sol_vault_pubkey": vault_meta.get("sol_vault_pubkey"),
        "sol_vault_kind": vault_meta.get("sol_vault_kind"),
        "vault_drained": bool(vault_meta.get("vault_drained")),
        # 观测用：曲线里那截提不出来的虚拟 SOL（迁移池恒 ≈17.5845）
        "quote_virtual_sol": (
            float(vault_meta["quote_virtual_raw"]) / 1e9
            if vault_meta.get("quote_virtual_raw")
            and vault_meta.get("quote_mint") == WSOL_MINT
            else 0.0
        ),
    }
    _price_cache[mint] = row
    return row, ""


def apply_vault_sol_to_position(
    pos: dict[str, Any],
    sol_v: float | None,
    *,
    vault_drained: bool = False,
    mint: str = "",
) -> bool:
    """回写 sol_vault，并在相对开仓金库骤降时打 vault_drain。

    供 HTTP mark 与 WSS 推送共用。返回是否**新**触发抽池标记。
    """
    if sol_v is None and not vault_drained:
        return False
    if vault_drained and sol_v is None:
        sol_v = 0.0
    assert sol_v is not None
    pos["sol_vault"] = float(sol_v)
    entry_v = float(pos.get("entry_sol_vault") or 0)
    if entry_v <= 0 and float(sol_v) > 0:
        pos["entry_sol_vault"] = float(sol_v)
        return False
    if entry_v <= 0:
        return False
    if str(pos.get("track") or "").upper() == "E":
        drain_drop = float(
            getattr(C, "TRACK_E_VAULT_DRAIN_DROP_PCT", 0.20)
            or getattr(C, "VAULT_DRAIN_DROP_PCT", 0.40)
        )
    else:
        drain_drop = float(getattr(C, "VAULT_DRAIN_DROP_PCT", 0.40))
    drop = 1.0 - (float(sol_v) / entry_v)
    if not (drop >= drain_drop or vault_drained):
        return False
    already = bool(pos.get("vault_drain"))
    pos["vault_drain"] = True
    pos["vault_drain_drop"] = round(max(drop, 0.0), 4)
    if already:
        return False
    logger.error(
        "🚨 金库SOL骤降 %s：%.3f → %.3f SOL（-%.0f%% ≥ %.0f%%）— 标记抽池逃生",
        pos.get("symbol") or (mint[:6] if mint else "?"),
        entry_v,
        float(sol_v),
        drop * 100,
        drain_drop * 100,
    )
    return True


def sol_amount_from_account_data(
    data_b64: str | bytes | None,
    *,
    kind: str = "spl",
) -> float | None:
    """从 accountSubscribe 推送的 base64 账户数据解析 SOL 数量。"""
    if data_b64 is None:
        return None
    try:
        if isinstance(data_b64, bytes):
            raw = data_b64
        else:
            raw = base64.b64decode(data_b64)
    except Exception:
        return None
    if kind == "bonding":
        if len(raw) < 40:
            return None
        return float(struct.unpack_from("<Q", raw, 32)[0]) / 1e9
    # SPL Token Account：amount u64 @64
    if len(raw) < 72:
        return None
    return float(struct.unpack_from("<Q", raw, 64)[0]) / 1e9


def fetch_prices_for_positions(positions: dict[str, dict[str, Any]]) -> dict[str, float]:
    """批量刷新持仓链上价 → {mint: price_sol}。

    同时回写 sol_vault，并在相对开仓金库 SOL 骤降时打上 vault_drain 标记，
    供 manage() 抢在假价/过期 mark 之前强制逃生（CXMT 类）。

    读不到价时打 mark_stale_since 计时（成功即清零）：过期 mark 不再是
    「本轮跳过」这种静默行为，而是一个上层能看见、超时会逼平仓的状态
    （NOTCOON 类：meteoradbc 池全程读不出价，止损止盈对着不动的价空转）。
    """
    out: dict[str, float] = {}
    now = time.time()
    for mint, pos in positions.items():
        row, reason = fetch_pool_price_row(
            mint,
            pool=pos.get("pool"),
            dex=pos.get("dex"),
        )
        if row and row.get("price"):
            out[mint] = float(row["price"])
            pos.pop("mark_stale_since", None)
            pos.pop("mark_stale_reason", None)
            if row.get("pool") and not pos.get("pool"):
                pos["pool"] = row["pool"]
            pos["price_source"] = row.get("source")
            pos["price_ts"] = row.get("ts")
            if row.get("sol_vault_pubkey"):
                pos["sol_vault_pubkey"] = row["sol_vault_pubkey"]
            if row.get("sol_vault_kind"):
                pos["sol_vault_kind"] = row["sol_vault_kind"]
            apply_vault_sol_to_position(
                pos,
                row.get("sol_vault"),
                vault_drained=bool(row.get("vault_drained")),
                mint=mint,
            )
        else:
            pos["mark_stale_reason"] = reason
            first = float(pos.get("mark_stale_since") or 0)
            if first <= 0:
                pos["mark_stale_since"] = now
                logger.error(
                    "🚨 持仓 %s 链上报价失败（%s）— mark 已冻结，开始计时逃生",
                    pos.get("symbol") or mint[:8],
                    reason,
                )
            else:
                logger.warning(
                    "持仓 %s 链上报价仍失败（%s）已 %.0fs — mark 仍是 %.10g",
                    pos.get("symbol") or mint[:8],
                    reason,
                    now - first,
                    float(pos.get("mark") or 0),
                )
    return out


# ---------------------------------------------------------------- 批量池快照
# 池布局缓存：base/quote vault 地址与 mint decimals 建池后不再变，
# 每轮重读就是白烧 RPC 配额（120 池 ≈ 360 个 key ≈ 4 个 getMultipleAccounts）。
# 只有金库余额每轮必须重读，故缓存布局后稳态降到 pool 2 + vault 3 ≈ 5 次/轮。
_pool_layout_cache: dict[str, dict[str, Any]] = {}
_mint_decimals_cache: dict[str, int] = {}

# getMultipleAccounts 单次上限 100
_RPC_CHUNK = 100


def _chunked_accounts(keys: list[str]) -> dict[str, dict[str, Any] | None]:
    """分批读账户 → {pubkey: value}。任一批失败即抛 RpcError，由调用方降级。"""
    out: dict[str, dict[str, Any] | None] = {}
    for i in range(0, len(keys), _RPC_CHUNK):
        part = keys[i : i + _RPC_CHUNK]
        vals = rpc.get_multiple_accounts(part)
        for j, k in enumerate(part):
            out[k] = vals[j] if j < len(vals) else None
    return out


def _decode_pool_layout(pool: str, acc: dict[str, Any]) -> dict[str, Any]:
    """从池账户解出不变量（owner / vault / mint）。认不出的场所返回 kind=unknown。"""
    owner = str(acc.get("owner") or "")
    raw = _b64_data(acc) or b""
    if owner == PUMP_PROGRAM:
        return {"kind": "bonding_curve", "owner": owner}
    if owner == PUMPSWAP_PROGRAM:
        if len(raw) < 203:
            return {"kind": "short", "owner": owner}
        return {
            "kind": "pumpswap",
            "owner": owner,
            "base_mint": _pk_at(raw, _OFF_BASE_MINT),
            "quote_mint": _pk_at(raw, _OFF_QUOTE_MINT),
            "base_vault": _pk_at(raw, _OFF_BASE_VAULT),
            "quote_vault": _pk_at(raw, _OFF_QUOTE_VAULT),
        }
    if owner == METEORA_DBC_PROGRAM:
        if len(raw) < _DBC_MIN_LEN:
            return {"kind": "short", "owner": owner}
        return {
            "kind": "meteora_dbc",
            "owner": owner,
            "base_mint": _pk_at(raw, _DBC_OFF_BASE_MINT),
            "base_vault": _pk_at(raw, _DBC_OFF_BASE_VAULT),
            "quote_vault": _pk_at(raw, _DBC_OFF_QUOTE_VAULT),
        }
    return {"kind": "unknown", "owner": owner}


def batch_pool_snapshots(pools: list[str]) -> dict[str, dict[str, Any]]:
    """一次性读一批池 → {pool: {price, sol_vault, owner, source, reason}}。

    price 为 None 时 reason 必然非空——「读不到」和「价归零」是两件事，
    上层（观察池自采序列 / 持仓逃生）必须能分开处理，绝不能静默跳过。
    抽干池仍回 1e-18 哨兵并置 reason=vault_drained：给持仓逃生用，
    自采序列侧必须按 reason 丢弃（假暴跌会直接喂出假回升）。
    """
    uniq = [p for p in dict.fromkeys(pools) if p]
    out: dict[str, dict[str, Any]] = {}
    if not uniq:
        return out
    try:
        pool_accounts = _chunked_accounts(uniq)
    except rpc.RpcError as exc:
        logger.warning("批量读池失败（%d 个）: %s", len(uniq), exc)
        return out

    layouts: dict[str, dict[str, Any]] = {}
    vault_need: list[str] = []
    mint_need: list[str] = []
    for pool in uniq:
        acc = pool_accounts.get(pool)
        if not acc:
            out[pool] = {"price": None, "reason": "empty_account", "owner": ""}
            continue
        layout = _pool_layout_cache.get(pool)
        if not layout or layout.get("owner") != str(acc.get("owner") or ""):
            layout = _decode_pool_layout(pool, acc)
            _pool_layout_cache[pool] = layout
        layouts[pool] = layout
        kind = layout.get("kind")
        if kind in ("pumpswap", "meteora_dbc"):
            vault_need.extend(
                [x for x in (layout.get("base_vault"), layout.get("quote_vault")) if x]
            )
            for m in (layout.get("base_mint"), layout.get("quote_mint")):
                if m and m != WSOL_MINT and m not in _mint_decimals_cache:
                    mint_need.append(m)

    fetch = list(dict.fromkeys(vault_need + mint_need))
    vault_accounts: dict[str, dict[str, Any] | None] = {}
    if fetch:
        try:
            vault_accounts = _chunked_accounts(fetch)
        except rpc.RpcError as exc:
            logger.warning("批量读 vault 失败（%d 个）: %s", len(fetch), exc)
            return out
        for m in mint_need:
            dec = _mint_decimals(vault_accounts.get(m))
            if dec is not None:
                _mint_decimals_cache[m] = dec

    for pool in uniq:
        if pool in out:
            continue
        layout = layouts.get(pool) or {}
        kind = layout.get("kind")
        owner = str(layout.get("owner") or "")
        acc = pool_accounts.get(pool)
        if kind == "bonding_curve":
            price = price_from_bonding_curve_account(acc or {})
            sol_vault = None
            raw_bc = _b64_data(acc) or b""
            if len(raw_bc) >= 40:
                sol_vault = struct.unpack_from("<Q", raw_bc, 32)[0] / 1e9
            out[pool] = {
                "price": price,
                "sol_vault": sol_vault,
                "owner": owner,
                "source": "pump_bonding_curve",
                "reason": "" if price and price > 0 else "decode_fail",
            }
        elif kind == "pumpswap":
            base_mint = str(layout.get("base_mint") or "")
            quote_mint = str(layout.get("quote_mint") or "")
            token_mint = quote_mint if base_mint == WSOL_MINT else base_mint
            dec = _mint_decimals_cache.get(token_mint, _DEFAULT_TOKEN_DECIMALS)
            price, meta = price_from_pumpswap_pool(
                acc or {}, vault_accounts=vault_accounts, token_decimals=dec
            )
            reason = ""
            if meta.get("vault_drained"):
                reason = "vault_drained"
            elif WSOL_MINT not in (base_mint, quote_mint):
                reason = "non_sol_quote"
            elif price is None or price <= 0:
                reason = "decode_fail"
            out[pool] = {
                "price": price,
                "sol_vault": meta.get("sol_vault"),
                "owner": owner,
                "source": "pumpswap_vaults",
                "reason": reason,
            }
        elif kind == "meteora_dbc":
            dec = _mint_decimals_cache.get(
                str(layout.get("base_mint") or ""), _DEFAULT_TOKEN_DECIMALS
            )
            price, meta = price_from_meteora_dbc_pool(
                acc or {}, vault_accounts=vault_accounts, base_decimals=dec
            )
            reason = ""
            if meta.get("dbc_migrated"):
                reason = "dbc_migrated"
            elif meta.get("vault_drained"):
                reason = "vault_drained"
            elif meta.get("quote_mint") is not None and meta["quote_mint"] != WSOL_MINT:
                reason = "non_sol_quote"
            elif price is None or price <= 0:
                reason = "decode_fail"
            out[pool] = {
                "price": price,
                "sol_vault": meta.get("sol_vault"),
                "owner": owner,
                "source": "meteora_dbc",
                "reason": reason,
            }
        else:
            out[pool] = {
                "price": None,
                "sol_vault": None,
                "owner": owner,
                "source": "unknown",
                "reason": f"unknown_owner:{owner[:8]}" if owner else "decode_fail",
            }
    return out


def batch_pool_depth_sol(pools: list[str]) -> dict[str, float]:
    """批量真实报价侧深度 → {pool: quote 金库真实 SOL}。读不出的 key 直接缺席。

    「真实」二字是重点：只算金库里提得出来的 SOL，不含虚拟储备，也不采信
    DexScreener 报的 liquidity——盘面上 100 SOL 深度、金库里 2e-9 SOL 的
    诱饵池就是靠这一条拦下来的。
    """
    snaps = batch_pool_snapshots(pools)
    out: dict[str, float] = {}
    for pool, snap in snaps.items():
        sol = snap.get("sol_vault")
        if sol is not None:
            out[pool] = float(sol)
    return out


def refresh_candidate_prices(candidates: list[dict[str, Any]], *, limit: int = 12) -> int:
    """批量链上刷新候选现价（一次 getMultipleAccounts），就地更新，返回成功条数。"""
    rows = [r for r in candidates[: max(0, int(limit))] if r.get("mint")]
    if not rows:
        return 0

    # 补齐缺失 pool
    for row in rows:
        if not row.get("pool"):
            pool = resolve_pool_for_mint(str(row["mint"]), None)
            if pool:
                row["pool"] = pool

    pools = [str(r["pool"]) for r in rows if r.get("pool")]
    if not pools:
        return 0

    try:
        pool_accounts = rpc.get_multiple_accounts(pools)
    except rpc.RpcError as exc:
        logger.warning("候选批量读池失败: %s", exc)
        return 0

    pool_by_addr = {pools[i]: pool_accounts[i] for i in range(len(pools))}

    # PumpSwap / Meteora DBC 需要二次读 vault
    vault_need: list[str] = []
    pumpswap_meta: dict[str, dict[str, str]] = {}
    dbc_meta: dict[str, dict[str, str]] = {}
    for addr, acc in pool_by_addr.items():
        if not acc:
            continue
        acc_owner = str(acc.get("owner") or "")
        raw = _b64_data(acc) or b""
        if acc_owner == PUMPSWAP_PROGRAM:
            if len(raw) < 203:
                continue
            meta = {
                "base_mint": _pk_at(raw, _OFF_BASE_MINT),
                "quote_mint": _pk_at(raw, _OFF_QUOTE_MINT),
                "base_vault": _pk_at(raw, _OFF_BASE_VAULT),
                "quote_vault": _pk_at(raw, _OFF_QUOTE_VAULT),
            }
            pumpswap_meta[addr] = meta
            vault_need.extend(
                [meta["base_vault"], meta["quote_vault"], meta["base_mint"], meta["quote_mint"]]
            )
        elif acc_owner == METEORA_DBC_PROGRAM:
            if len(raw) < _DBC_MIN_LEN:
                continue
            meta = {
                "base_mint": _pk_at(raw, _DBC_OFF_BASE_MINT),
                "base_vault": _pk_at(raw, _DBC_OFF_BASE_VAULT),
                "quote_vault": _pk_at(raw, _DBC_OFF_QUOTE_VAULT),
            }
            dbc_meta[addr] = meta
            vault_need.extend([meta["base_mint"], meta["base_vault"], meta["quote_vault"]])

    vault_accounts: dict[str, dict[str, Any] | None] = {}
    if vault_need:
        # 去重保序
        uniq: list[str] = []
        seen: set[str] = set()
        for a in vault_need:
            if a not in seen:
                seen.add(a)
                uniq.append(a)
        try:
            vals = rpc.get_multiple_accounts(uniq)
            vault_accounts = {uniq[i]: vals[i] for i in range(len(uniq))}
        except rpc.RpcError as exc:
            logger.warning("候选批量读 vault 失败: %s", exc)

    updated = 0
    now = time.time()
    for row in rows:
        pool = row.get("pool")
        if not pool:
            continue
        acc = pool_by_addr.get(str(pool))
        if not acc:
            continue
        owner = str(acc.get("owner") or "")
        price: float | None = None
        source = "unknown"
        if owner == PUMP_PROGRAM:
            price = price_from_bonding_curve_account(acc)
            source = "pump_bonding_curve"
        elif owner == PUMPSWAP_PROGRAM:
            meta = pumpswap_meta.get(str(pool)) or {}
            token_mint = str(row.get("mint") or "")
            # decimals：优先 token mint 账户
            dec = _DEFAULT_TOKEN_DECIMALS
            for m in (meta.get("base_mint"), meta.get("quote_mint")):
                if m and m != WSOL_MINT and m == token_mint:
                    d = _mint_decimals(vault_accounts.get(m))
                    if d is not None:
                        dec = d
                    break
            price, pmeta = price_from_pumpswap_pool(
                acc,
                vault_accounts=vault_accounts,
                token_decimals=dec,
            )
            source = "pumpswap_vaults"
            if pmeta.get("vault_drained"):
                # 抽干哨兵（1e-18）只给持仓逃生用；写进候选板会污染
                # 自采价格序列（假暴跌→之后假回升）。这里直接跳过不更新。
                continue
        elif owner == METEORA_DBC_PROGRAM:
            meta = dbc_meta.get(str(pool)) or {}
            dec = _mint_decimals(vault_accounts.get(meta.get("base_mint") or ""))
            price, pmeta = price_from_meteora_dbc_pool(
                acc,
                vault_accounts=vault_accounts,
                base_decimals=dec if dec is not None else _DEFAULT_TOKEN_DECIMALS,
            )
            source = "meteora_dbc"
            if pmeta.get("vault_drained"):
                continue
        if price is None or price <= 0:
            continue

        prev = float(row.get("price") or 0)
        row["price"] = float(price)
        row["price_repr"] = f"{float(price):.18g}"
        row["price_source"] = source
        row["price_ts"] = now
        from .strategy import apply_price_drawdown

        apply_price_drawdown(row, float(price))
        if prev > 0:
            chg = (float(price) - prev) / prev
            row["price_chg_pct"] = round(chg * 100.0, 4)
            row["price_dir"] = "up" if chg > 1e-12 else ("down" if chg < -1e-12 else "flat")
        else:
            row["price_chg_pct"] = 0.0
            row["price_dir"] = "flat"
        _price_cache[str(row["mint"])] = {
            "mint": row["mint"],
            "pool": pool,
            "owner": owner,
            "price": float(price),
            "source": source,
            "ts": now,
        }
        updated += 1
    return updated


def cached_price(mint: str) -> float | None:
    row = _price_cache.get(mint)
    if not row:
        return None
    return float(row["price"]) if row.get("price") else None


def cached_meta(mint: str) -> dict[str, Any] | None:
    return _price_cache.get(mint)
