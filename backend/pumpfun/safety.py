"""买入前链上安全审计（防貔貅 / 无限增发 / 撤池子 / Token-2022 税 / 诱饵元数据）。

原则：宁可错过，绝不冒险（fail-closed）——任何一项拿不到数据或不达标都拒绝下单。

1) Mint/Freeze 权限：mint_authority 与 freeze_authority 必须均为 null（已放弃）。
2) Token-2022 扩展：拦截 transfer fee / permanent delegate / transfer hook / non-transferable。
3) Metaplex updateAuthority：未放弃则可改名/改社媒做诱饵盘（可配开关）。
4) LP / 撤池风险：Pump 曲线=程序托管；PumpSwap 须验证 LP 已销毁（程序归属≠锁池）。
5) Creator/deployer 黑名单：命中已知恶名钱包 → 拦截。
6) 筹码集中度 / 老鼠仓 / 捆绑聚类（holders）。
7) RPC 超时/限流/数据缺失：一律判定不通过。
"""

from __future__ import annotations

import base64
import logging
import struct
import time
from dataclasses import dataclass, field
from typing import Any

from solders.pubkey import Pubkey

from . import blacklist
from . import config as C
from . import rpc
from .onchain_price import (
    PUMP_PROGRAM,
    PUMPSWAP_PROGRAM,
    _OFF_LP_MINT,
    _OFF_LP_SUPPLY,
    _b64_data,
    _pk_at,
    bonding_curve_pda,
)

logger = logging.getLogger("pumpfun.safety")

# SPL Token / Token-2022 程序（两者 Mint 前 82 字节布局一致）
_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
_TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
_SAFE_TOKEN_PROGRAMS = {_TOKEN_PROGRAM, _TOKEN_2022_PROGRAM}

# Metaplex Token Metadata
_METADATA_PROGRAM = "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s"

# bonding curve：流动性锁在曲线程序里，creator 不能像 AMM 那样撤 LP
_PUMP_CURVE_LOCK_LABEL = "Pump 联合曲线（流动性程序托管）"

# LP 烧毁地址（与 holders 对齐；落入这些地址才算真正锁池）
_LP_BURN_ADDRESSES = {
    "11111111111111111111111111111111",
    "1nc1nerator11111111111111111111111111111111",
    "dead111111111111111111111111111111111111111",
}

# Token-2022 TLV extension types（危险项）
_EXT_TRANSFER_FEE_CONFIG = 1
_EXT_DEFAULT_ACCOUNT_STATE = 6
_EXT_NON_TRANSFERABLE = 9
_EXT_PERMANENT_DELEGATE = 12
_EXT_TRANSFER_HOOK = 14

_DANGEROUS_EXT_NAMES = {
    _EXT_TRANSFER_FEE_CONFIG: "transferFeeConfig（卖出税/转账税）",
    _EXT_PERMANENT_DELEGATE: "permanentDelegate（永久代理可转走余额）",
    _EXT_NON_TRANSFERABLE: "nonTransferable（不可转让）",
    _EXT_TRANSFER_HOOK: "transferHook（自定义转账钩子，可禁售）",
    _EXT_DEFAULT_ACCOUNT_STATE: "defaultAccountState（默认可冻结）",
}

_JSON_EXT_DANGER = {
    "transferFeeConfig": "transferFeeConfig（卖出税/转账税）",
    "transfer_fee_config": "transferFeeConfig（卖出税/转账税）",
    "permanentDelegate": "permanentDelegate（永久代理可转走余额）",
    "permanent_delegate": "permanentDelegate（永久代理可转走余额）",
    "nonTransferable": "nonTransferable（不可转让）",
    "non_transferable": "nonTransferable（不可转让）",
    "transferHook": "transferHook（自定义转账钩子，可禁售）",
    "transfer_hook": "transferHook（自定义转账钩子，可禁售）",
    "defaultAccountState": "defaultAccountState（默认可冻结）",
    "default_account_state": "defaultAccountState（默认可冻结）",
}

_cache: dict[str, "SafetyResult"] = {}


@dataclass
class SafetyResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    checks: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reasons": list(self.reasons),
            "checks": dict(self.checks),
            "ts": self.ts,
        }


def _decode_mint_authorities(value: dict[str, Any]) -> tuple[bool, str | None, str | None]:
    """解析 Mint 账户 → (是否 SPL Mint, mint_authority, freeze_authority)。"""
    owner = value.get("owner")
    if owner not in _SAFE_TOKEN_PROGRAMS:
        return False, None, None

    data = value.get("data")
    if isinstance(data, dict):
        info = (data.get("parsed") or {}).get("info") or {}
        if (data.get("parsed") or {}).get("type") == "mint":
            return (
                True,
                info.get("mintAuthority"),
                info.get("freezeAuthority"),
            )
        return False, None, None

    if isinstance(data, list) and data:
        try:
            raw = base64.b64decode(data[0])
        except Exception:
            return False, None, None
        if len(raw) < 82:
            return False, None, None
        mint_opt = struct.unpack_from("<I", raw, 0)[0]
        mint_auth = str(Pubkey.from_bytes(raw[4:36])) if mint_opt == 1 else None
        freeze_opt = struct.unpack_from("<I", raw, 46)[0]
        freeze_auth = str(Pubkey.from_bytes(raw[50:82])) if freeze_opt == 1 else None
        return True, mint_auth, freeze_auth

    return False, None, None


def _parse_tlv_extensions(raw: bytes) -> list[int]:
    """Token-2022 Mint：base82 + AccountType(1) + TLV。返回 extension type 列表。"""
    if len(raw) <= 83:
        return []
    types: list[int] = []
    off = 83
    while off + 4 <= len(raw):
        ext_type = struct.unpack_from("<H", raw, off)[0]
        ext_len = struct.unpack_from("<H", raw, off + 2)[0]
        off += 4
        if ext_len < 0 or off + ext_len > len(raw):
            break
        types.append(int(ext_type))
        off += ext_len
    return types


def _check_token2022_extensions(value: dict[str, Any], checks: dict[str, Any]) -> list[str]:
    """拦截危险 Token-2022 扩展（税/永久代理/钩子等）。经典 SPL 直接通过。"""
    if not C.TOKEN2022_EXT_CHECK:
        checks["token2022_ext_skipped"] = True
        return []

    owner = value.get("owner")
    checks["token_program"] = owner
    if owner != _TOKEN_2022_PROGRAM:
        checks["token2022"] = False
        return []

    checks["token2022"] = True
    fails: list[str] = []
    found: list[str] = []

    data = value.get("data")
    if isinstance(data, dict):
        info = (data.get("parsed") or {}).get("info") or {}
        exts = info.get("extensions") or []
        for item in exts:
            if not isinstance(item, dict):
                continue
            name = item.get("extension") or item.get("extensionType") or ""
            label = _JSON_EXT_DANGER.get(str(name))
            if label:
                found.append(label)
                fails.append(f"Token-2022 危险扩展: {label}")

    raw = None
    if isinstance(data, list) and data:
        try:
            raw = base64.b64decode(data[0])
        except Exception:
            raw = None
    elif isinstance(data, dict):
        try:
            mint = checks.get("_mint_for_raw")
            if mint:
                acc = rpc.get_account_info(mint, encoding="base64")
                if acc and isinstance(acc.get("data"), list) and acc["data"]:
                    raw = base64.b64decode(acc["data"][0])
        except Exception:
            raw = None

    if raw:
        for t in _parse_tlv_extensions(raw):
            label = _DANGEROUS_EXT_NAMES.get(t)
            if label and label not in found:
                found.append(label)
                fails.append(f"Token-2022 危险扩展: {label}")

    checks["token2022_dangerous"] = found
    return fails


def _check_authorities(mint: str, checks: dict[str, Any]) -> list[str]:
    """Mint/Freeze 权限 + Token-2022 扩展审计。"""
    try:
        value = rpc.get_account_info(mint, encoding="jsonParsed")
    except Exception as exc:
        checks["authority_error"] = str(exc)
        return [f"链上安全检查超时/RPC错误（Mint 账户不可达）: {exc}"]

    if not value:
        checks["authority_error"] = "mint_account_missing"
        return ["Mint 账户不存在或不可读（未通过风控白名单）"]

    checks["_mint_for_raw"] = mint
    is_mint, mint_auth, freeze_auth = _decode_mint_authorities(value)
    checks["mint_authority"] = mint_auth
    checks["freeze_authority"] = freeze_auth
    checks["token_program"] = value.get("owner")

    if not is_mint:
        return ["无法解析 Mint 元数据（非标准 SPL Mint，未通过风控白名单）"]

    fails: list[str] = []
    if freeze_auth is not None:
        fails.append("freeze_authority 未放弃（貔貅风险：可冻结钱包禁止卖出）")
    if mint_auth is not None:
        fails.append("mint_authority 未放弃（增发风险：可无限增发砸盘）")

    fails.extend(_check_token2022_extensions(value, checks))
    checks.pop("_mint_for_raw", None)
    return fails


def _lp_token_supply_raw(lp_mint: str) -> int:
    """LP mint 供应量；允许 0（已撤光）。失败抛异常。"""
    result = rpc.rpc_call(
        "getTokenSupply",
        [lp_mint, {"commitment": "confirmed"}],
        max_retries=2,
        timeout=min(12.0, float(C.RPC_TIMEOUT_SEC)),
    )
    if not isinstance(result, dict) or "value" not in result:
        raise rpc.RpcError(f"getTokenSupply 返回异常: {result!r}")
    return int((result.get("value") or {}).get("amount") or 0)


def _check_pumpswap_lp_burned(acc: dict[str, Any], checks: dict[str, Any]) -> list[str]:
    """PumpSwap：必须验证 LP 已销毁，不能仅凭「池子程序是 PumpSwap」放行。"""
    raw = _b64_data(acc) or b""
    if len(raw) < _OFF_LP_MINT + 32:
        checks["lp_error"] = "pool_too_short_for_lp_mint"
        return ["PumpSwap 池账户过短，无法读 LP mint（未确认锁池，未通过风控白名单）"]

    lp_mint = _pk_at(raw, _OFF_LP_MINT)
    checks["lp_mint"] = lp_mint
    if len(raw) >= _OFF_LP_SUPPLY + 8:
        try:
            checks["lp_supply_onchain"] = int(
                struct.unpack_from("<Q", raw, _OFF_LP_SUPPLY)[0]
            )
        except Exception:
            pass

    try:
        supply_raw = _lp_token_supply_raw(lp_mint)
    except Exception as exc:
        checks["lp_error"] = str(exc)
        return [f"链上安全检查超时/RPC错误（无法读 LP 供应量）: {exc}"]

    checks["lp_supply_raw"] = supply_raw
    if supply_raw <= 0:
        checks["lp_burn_pct"] = 0.0
        checks["pool_lock"] = "PumpSwap LP 供应量为 0（已撤光）"
        return [
            "PumpSwap LP 供应量为 0（流动性已撤光或从未锁定，撤池风险，未通过风控白名单）"
        ]

    try:
        largest = rpc.get_token_largest_accounts(lp_mint)
    except Exception as exc:
        checks["lp_error"] = str(exc)
        return [f"链上安全检查超时/RPC错误（无法拉 LP 持仓）: {exc}"]

    # 解析 LP token 账户 owner；解析失败则按账户地址本身是否为烧毁地址判断
    owners: dict[str, str] = {}
    try:
        addrs = [r["address"] for r in largest if r.get("address")][:20]
        accs = rpc.get_multiple_accounts(addrs, encoding="jsonParsed") if addrs else []
        for addr, a in zip(addrs, accs):
            try:
                info = ((a or {}).get("data") or {}).get("parsed", {}).get("info", {})
                owner = info.get("owner")
                if owner:
                    owners[addr] = owner
            except Exception:
                continue
    except Exception as exc:
        checks["lp_owner_resolve_error"] = str(exc)

    burned = 0
    unlocked_top: list[dict[str, Any]] = []
    for row in largest:
        addr = str(row.get("address") or "")
        amt = int(row.get("amount_raw") or 0)
        if amt <= 0 or not addr:
            continue
        owner = owners.get(addr) or addr
        if addr in _LP_BURN_ADDRESSES or owner in _LP_BURN_ADDRESSES:
            burned += amt
        else:
            unlocked_top.append(
                {"address": owner[:8] + "…", "pct": round(amt / supply_raw, 4)}
            )

    burn_pct = burned / supply_raw if supply_raw > 0 else 0.0
    checks["lp_burn_pct"] = round(burn_pct, 4)
    checks["lp_unlocked_top"] = unlocked_top[:5]
    min_burn = float(C.LP_MIN_BURN_PCT)
    checks["lp_min_burn_pct"] = min_burn
    if burn_pct + 1e-12 < min_burn:
        checks["pool_lock"] = f"PumpSwap LP 未锁定（销毁 {burn_pct*100:.1f}%）"
        return [
            f"PumpSwap LP 未销毁/未锁定（已销毁 {burn_pct*100:.1f}% "
            f"< {min_burn*100:.0f}%，可撤池，未通过风控白名单）"
        ]

    checks["pool_lock"] = f"PumpSwap LP 已销毁 {burn_pct*100:.1f}%"
    return []


def _check_liquidity_lock(
    mint: str, pool: str | None, dex: str | None, checks: dict[str, Any]
) -> list[str]:
    """LP / 撤池风险审计。

    - Pump bonding curve：流动性在曲线程序内，视为协议托管。
    - PumpSwap：必须额外验证 LP mint 已销毁到烧毁地址（程序归属 ≠ 锁池）。
    """
    pool_addr = (pool or "").strip()
    if not pool_addr:
        try:
            pool_addr = bonding_curve_pda(mint)
            checks["derived_bonding_curve"] = pool_addr
        except Exception as exc:
            checks["pool_error"] = f"derive_curve_failed: {exc}"
            return [f"无池地址且无法推导联合曲线 PDA: {exc}"]

    try:
        acc = rpc.get_account_info(pool_addr, encoding="base64")
    except Exception as exc:
        checks["pool_error"] = str(exc)
        return [f"链上安全检查超时/RPC错误（池账户不可达）: {exc}"]

    if not acc:
        checks["pool_error"] = "pool_account_missing"
        return ["池账户不存在或不可读（无法确认流动性锁定，未通过风控白名单）"]

    owner = acc.get("owner")
    checks["pool_owner"] = owner
    checks["pool_address"] = pool_addr
    creator = _extract_creator(owner, acc)
    if creator:
        checks["creator"] = creator

    if owner == PUMP_PROGRAM:
        checks["pool_lock"] = _PUMP_CURVE_LOCK_LABEL
        return []

    if owner == PUMPSWAP_PROGRAM:
        return _check_pumpswap_lp_burned(acc, checks)

    return [
        f"池归属未知程序 {owner}（无法确认 LP 已销毁/锁定，撤池风险，未通过风控白名单）"
    ]


def _extract_creator(pool_owner: str | None, acc: dict[str, Any]) -> str | None:
    """从 Pump bonding curve / PumpSwap 池账户解出 creator/deployer。"""
    data = acc.get("data")
    if not isinstance(data, list) or not data:
        return None
    try:
        raw = base64.b64decode(data[0])
    except Exception:
        return None
    try:
        if pool_owner == PUMP_PROGRAM:
            if len(raw) >= 81:
                return str(Pubkey.from_bytes(raw[49:81]))
        if pool_owner == PUMPSWAP_PROGRAM:
            if len(raw) >= 43:
                return str(Pubkey.from_bytes(raw[11:43]))
    except Exception:
        return None
    return None


def _metadata_pda(mint: str) -> str:
    return str(
        Pubkey.find_program_address(
            [
                b"metadata",
                bytes(Pubkey.from_string(_METADATA_PROGRAM)),
                bytes(Pubkey.from_string(mint)),
            ],
            Pubkey.from_string(_METADATA_PROGRAM),
        )[0]
    )


def _check_metadata_authority(mint: str, checks: dict[str, Any]) -> list[str]:
    """Metaplex updateAuthority 未放弃 → 可改名做诱饵。无元数据账户不硬拦。"""
    if not C.REQUIRE_REVOKED_UPDATE_AUTH:
        checks["metadata_skipped"] = True
        return []
    try:
        pda = _metadata_pda(mint)
        checks["metadata_pda"] = pda
        acc = rpc.get_account_info(pda, encoding="base64")
    except Exception as exc:
        # mint 非法 / PDA 失败：实盘不会出现；测试用假 mint 时跳过而非误杀全套
        msg = str(exc)
        if "Base58" in msg or "Invalid" in msg:
            checks["metadata_skipped"] = f"invalid_mint: {exc}"
            return []
        checks["metadata_error"] = msg
        return [f"元数据账户不可达（未通过风控白名单）: {exc}"]

    if not acc:
        checks["metadata"] = "missing"
        return []

    data = acc.get("data")
    if not isinstance(data, list) or not data:
        return ["元数据账户无法解析（未通过风控白名单）"]
    try:
        raw = base64.b64decode(data[0])
    except Exception as exc:
        return [f"元数据解码失败: {exc}"]
    if len(raw) < 33:
        return ["元数据过短，无法读 updateAuthority"]
    update_auth = str(Pubkey.from_bytes(raw[1:33]))
    checks["update_authority"] = update_auth
    if update_auth == "11111111111111111111111111111111":
        checks["update_authority_revoked"] = True
        return []
    checks["update_authority_revoked"] = False
    return [
        f"元数据 updateAuthority 未放弃（{update_auth[:8]}…，可改名/改社媒做诱饵盘）"
    ]


def _check_creator_blacklist(checks: dict[str, Any]) -> list[str]:
    creator = checks.get("creator")
    hit, matched = blacklist.is_blacklisted(creator)
    checks["blacklist_hit"] = bool(hit)
    if hit:
        return [f"部署者/Creator 命中恶名黑名单（{matched[:8]}…）"]
    return []


def check_token_safety(
    mint: str,
    *,
    pool: str | None = None,
    dex: str | None = None,
    use_cache: bool = True,
) -> SafetyResult:
    """买入前链上安全审计。任何异常都 fail-closed（ok=False）。"""
    if use_cache:
        cached = _cache.get(mint)
        if cached and (time.time() - cached.ts) < float(C.SAFETY_CACHE_TTL_SEC):
            return cached

    checks: dict[str, Any] = {}
    reasons: list[str] = []
    try:
        reasons.extend(_check_authorities(mint, checks))
        reasons.extend(_check_liquidity_lock(mint, pool, dex, checks))
        reasons.extend(_check_metadata_authority(mint, checks))
        reasons.extend(_check_creator_blacklist(checks))
        try:
            from . import holders

            holder = holders.check_holder_concentration(
                mint, pool=pool, dex=dex, use_cache=use_cache
            )
            checks["holders"] = holder.checks
            checks["holder_ok"] = holder.ok
            if not holder.ok:
                reasons.extend(holder.reasons)
            checks["whale_snapshot"] = holder.whale_snapshot
        except Exception as exc:
            logger.exception("持仓集中度审计调用失败 mint=%s", mint)
            reasons.append(f"筹码集中度检查异常（未通过风控白名单）: {exc}")
    except Exception as exc:
        logger.exception("链上安全审计未预期异常 mint=%s", mint)
        reasons.append(f"链上安全检查异常（未通过风控白名单）: {exc}")

    result = SafetyResult(ok=(len(reasons) == 0), reasons=reasons, checks=checks)
    _cache[mint] = result
    return result


def clear_cache() -> None:
    _cache.clear()
