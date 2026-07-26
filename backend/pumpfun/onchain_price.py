"""持仓秒级链上报价：直接读 Pump.fun BondingCurve / PumpSwap 池账户。

废弃 DexScreener 持仓喂价。价格来源：
1) pool 账户 owner = pump bonding-curve → 解码 virtual reserves
2) pool 账户 owner = PumpSwap AMM → 读两侧 vault SPL token amount 比值
3) 必要时由 mint 推导 bonding-curve PDA
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
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

# PumpSwap Pool（8 字节 discriminator 之后）
# bump u8 + index u16 + creator/base/quote/lp/base_vault/quote_vault pubkeys
_OFF_BASE_MINT = 43
_OFF_QUOTE_MINT = 75
_OFF_BASE_VAULT = 139
_OFF_QUOTE_VAULT = 171

# 默认 pump token decimals；mint 账户读失败时回退
_DEFAULT_TOKEN_DECIMALS = 6

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
    """返回 (进度%, 来源说明)。pumpswap / 已毕业 → 100；读失败 → (None, reason)。"""
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
        # 未知程序：不当作极早期曲线，放行由其它风控管
        return 100.0, f"other_owner:{owner[:8]}"
    pct = bonding_curve_progress_pct(acc)
    if pct is None:
        return None, "decode_fail"
    return pct, "bonding_curve"


def price_from_pumpswap_pool(
    pool_account: dict[str, Any],
    *,
    vault_accounts: dict[str, dict[str, Any] | None] | None = None,
    token_decimals: int = _DEFAULT_TOKEN_DECIMALS,
) -> tuple[float | None, dict[str, Any]]:
    """返回 (price_sol, meta)。

    meta 恒含 base/quote mint+vault，以及 sol_vault（WSOL 侧 UI 数量）。
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
    meta = {
        "base_mint": base_mint,
        "quote_mint": quote_mint,
        "base_vault": base_vault,
        "quote_vault": quote_vault,
    }
    vaults = vault_accounts or {}
    base_amt = _spl_amount(vaults.get(base_vault))
    quote_amt = _spl_amount(vaults.get(quote_vault))
    # WSOL 侧绝对深度：砸盘抽干时第一信号（比价格比值更早、更稳）
    if base_mint == WSOL_MINT and base_amt is not None:
        meta["sol_vault"] = float(base_amt) / 1e9
    elif quote_mint == WSOL_MINT and quote_amt is not None:
        meta["sol_vault"] = float(quote_amt) / 1e9
    else:
        meta["sol_vault"] = None

    if base_amt is None or quote_amt is None or base_amt <= 0 or quote_amt <= 0:
        # SOL 侧被砸干：给一个近零价，逼硬止损/逃生，绝不能让上层沿用旧 mark
        if meta.get("sol_vault") is not None and float(meta["sol_vault"]) <= 0.05:
            meta["vault_drained"] = True
            return 1e-18, meta
        return None, meta
    dec = max(0, int(token_decimals))
    if base_mint == WSOL_MINT:
        price = (base_amt / 1e9) / (quote_amt / (10**dec))
    elif quote_mint == WSOL_MINT:
        price = (quote_amt / 1e9) / (base_amt / (10**dec))
    else:
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
    pool_addr = resolve_pool_for_mint(mint, pool)
    if not pool_addr:
        return None
    try:
        pool_acc = rpc.get_account_info(pool_addr)
    except rpc.RpcError as exc:
        logger.warning("链上读池失败 mint=%s pool=%s: %s", mint[:8], pool_addr[:8], exc)
        return None
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
            return None

    owner = str(pool_acc.get("owner") or "")
    price: float | None = None
    source = "unknown"
    vault_meta: dict[str, Any] = {}

    if owner == PUMP_PROGRAM:
        price = price_from_bonding_curve_account(pool_acc)
        source = "pump_bonding_curve"
        # bonding curve 的 real_sol 可近似当「池内 SOL」
        try:
            raw_bc = _b64_data(pool_acc) or b""
            if len(raw_bc) >= 40:
                vault_meta["sol_vault"] = struct.unpack_from("<Q", raw_bc, 32)[0] / 1e9
        except Exception:
            pass
    elif owner == PUMPSWAP_PROGRAM:
        raw = _b64_data(pool_acc) or b""
        if len(raw) < 203:
            return None
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
            return None
        vault_map = {base_vault: accounts[0], quote_vault: accounts[1]}
        decimals = _mint_decimals(accounts[2]) or _DEFAULT_TOKEN_DECIMALS
        price, vault_meta = price_from_pumpswap_pool(
            pool_acc, vault_accounts=vault_map, token_decimals=decimals
        )
        source = "pumpswap_vaults"
        if vault_meta.get("vault_drained"):
            source = "pumpswap_drained"
    else:
        logger.warning(
            "未知池 owner=%s mint=%s pool=%s dex=%s", owner[:12], mint[:8], pool_addr[:8], dex
        )
        return None

    # 抽干时 price 可能是近零哨兵；仍要回传让持仓管理能看到 sol_vault
    if (price is None or price <= 0) and not vault_meta.get("vault_drained"):
        return None

    row = {
        "mint": mint,
        "pool": pool_addr,
        "owner": owner,
        "price": float(price or 1e-18),
        "source": source,
        "dex": dex,
        "ts": time.time(),
        "sol_vault": vault_meta.get("sol_vault"),
        "vault_drained": bool(vault_meta.get("vault_drained")),
    }
    _price_cache[mint] = row
    return row


def fetch_prices_for_positions(positions: dict[str, dict[str, Any]]) -> dict[str, float]:
    """批量刷新持仓链上价 → {mint: price_sol}。

    同时回写 sol_vault，并在相对开仓金库 SOL 骤降时打上 vault_drain 标记，
    供 manage() 抢在假价/过期 mark 之前强制逃生（CXMT 类）。
    """
    out: dict[str, float] = {}
    drain_drop = float(getattr(C, "VAULT_DRAIN_DROP_PCT", 0.40))
    for mint, pos in positions.items():
        row = fetch_pool_price_sol(
            mint,
            pool=pos.get("pool"),
            dex=pos.get("dex"),
        )
        if row and row.get("price"):
            out[mint] = float(row["price"])
            if row.get("pool") and not pos.get("pool"):
                pos["pool"] = row["pool"]
            pos["price_source"] = row.get("source")
            pos["price_ts"] = row.get("ts")
            sol_v = row.get("sol_vault")
            if sol_v is not None:
                pos["sol_vault"] = float(sol_v)
                entry_v = float(pos.get("entry_sol_vault") or 0)
                if entry_v <= 0 and float(sol_v) > 0:
                    # 旧仓位没有开仓快照：用首次读到的当基线，下一轮才判骤降
                    pos["entry_sol_vault"] = float(sol_v)
                elif entry_v > 0:
                    drop = 1.0 - (float(sol_v) / entry_v)
                    if drop >= drain_drop or row.get("vault_drained"):
                        pos["vault_drain"] = True
                        pos["vault_drain_drop"] = round(drop, 4)
                        logger.error(
                            "🚨 金库SOL骤降 %s：%.3f → %.3f SOL（-%.0f%% ≥ %.0f%%）— 标记抽池逃生",
                            pos.get("symbol") or mint[:6],
                            entry_v,
                            float(sol_v),
                            drop * 100,
                            drain_drop * 100,
                        )
        else:
            logger.warning("持仓 %s 链上报价失败，本轮跳过", mint[:8])
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

    # PumpSwap 需要二次读 vault
    vault_need: list[str] = []
    pumpswap_meta: dict[str, dict[str, str]] = {}
    for addr, acc in pool_by_addr.items():
        if not acc or str(acc.get("owner") or "") != PUMPSWAP_PROGRAM:
            continue
        raw = _b64_data(acc) or b""
        if len(raw) < 203:
            continue
        meta = {
            "base_mint": _pk_at(raw, _OFF_BASE_MINT),
            "quote_mint": _pk_at(raw, _OFF_QUOTE_MINT),
            "base_vault": _pk_at(raw, _OFF_BASE_VAULT),
            "quote_vault": _pk_at(raw, _OFF_QUOTE_VAULT),
        }
        pumpswap_meta[addr] = meta
        vault_need.extend([meta["base_vault"], meta["quote_vault"], meta["base_mint"], meta["quote_mint"]])

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
            price, _ = price_from_pumpswap_pool(
                acc,
                vault_accounts=vault_accounts,
                token_decimals=dec,
            )
            source = "pumpswap_vaults"
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
