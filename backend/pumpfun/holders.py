"""筹码集中度与老鼠仓防御（Holder Concentration & Early Whale Dump）。

买入前：getTokenLargestAccounts 拉前大持仓，剔除流动性账户后，
前 10 大合计占供应量超过阈值 → 一票否决。

农场盘（CXMT）：不看前20持仓榜——扫池子最近成交，同 slot / 近窗内
多钱包等额齐买或齐卖 → 拒买。

开仓后 1~2 分钟：对比开仓时大户快照，若净流出超阈值 → 闪电平仓，
不等到硬止损 -13%。

Fail-closed：RPC 失败 / 数据缺失一律判定不通过或跳过开仓。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from . import config as C
from . import rpc
from .onchain_price import (
    PUMP_PROGRAM,
    PUMPSWAP_PROGRAM,
    WSOL_MINT,
    _OFF_BASE_VAULT,
    _OFF_QUOTE_VAULT,
    _b64_data,
    _pk_at,
    bonding_curve_pda,
)

logger = logging.getLogger("pumpfun.holders")

# 已知烧毁 / 黑洞地址（不计入控盘筹码，也不进大户快照）
_BURN_ADDRESSES = {
    "11111111111111111111111111111111",
    "1nc1nerator11111111111111111111111111111111",
    "dead111111111111111111111111111111111111111",
}

_cache: dict[str, "HolderResult"] = {}


@dataclass
class HolderResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    checks: dict[str, Any] = field(default_factory=dict)
    # 开仓后监控用：非流动性大户地址 → amount_raw
    whale_snapshot: dict[str, int] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reasons": list(self.reasons),
            "checks": dict(self.checks),
            "whale_count": len(self.whale_snapshot),
            "ts": self.ts,
        }


def _liquidity_token_accounts(mint: str, pool: str | None) -> set[str]:
    """收集应剔除的流动性 Token 账户（bonding curve / PumpSwap vault）。"""
    exclude: set[str] = set(_BURN_ADDRESSES)
    try:
        curve = bonding_curve_pda(mint)
        exclude.add(curve)
    except Exception:
        pass

    pool_addr = (pool or "").strip()
    if not pool_addr:
        return exclude

    try:
        acc = rpc.get_account_info(pool_addr, encoding="base64")
    except Exception:
        return exclude
    if not acc:
        return exclude

    owner = acc.get("owner")
    # Pump 联合曲线账户本身就是最大 token 持有方
    if owner == PUMP_PROGRAM:
        exclude.add(pool_addr)
        return exclude

    if owner == PUMPSWAP_PROGRAM:
        raw = _b64_data(acc) or b""
        if len(raw) >= 203:
            exclude.add(_pk_at(raw, _OFF_BASE_VAULT))
            exclude.add(_pk_at(raw, _OFF_QUOTE_VAULT))
        exclude.add(pool_addr)
    return exclude


def _resolve_owners(token_accounts: list[str]) -> dict[str, str]:
    """Token 账户地址 → 控制人钱包(owner)。解析失败返回空 dict（调用方回退账户级）。"""
    if not token_accounts:
        return {}
    out: dict[str, str] = {}
    try:
        # 一次最多解析 20 个（getTokenLargestAccounts 上限），批量读
        accs = rpc.get_multiple_accounts(token_accounts[:20], encoding="jsonParsed")
    except Exception as exc:
        logger.warning("解析持仓 owner 失败（回退账户级）: %s", exc)
        return {}
    for addr, acc in zip(token_accounts, accs):
        try:
            info = (acc or {}).get("data", {}).get("parsed", {}).get("info", {})
            owner = info.get("owner")
            if owner:
                out[addr] = owner
        except Exception:
            continue
    return out


def _find_funder(wallet: str) -> str | None:
    """找钱包最早一笔交易里的 SOL 出资方（净转出最多且非本钱包）。

    捆绑小号通常由同一母钱包批量打 SOL 激活，共同资金源即控盘证据。
    """
    try:
        sigs = rpc.get_signatures_for_address(wallet, limit=1000)
    except Exception:
        return None
    if not sigs:
        return None
    oldest = sigs[-1].get("signature")
    if not oldest:
        return None
    try:
        meta = rpc.get_transaction_meta(oldest)
    except Exception:
        return None
    if not meta:
        return None
    keys = meta.get("account_keys") or []
    pre = meta.get("pre_balances") or []
    post = meta.get("post_balances") or []
    best_funder = None
    best_out = 0
    for i, k in enumerate(keys):
        if i >= len(pre) or i >= len(post) or k == wallet:
            continue
        sent = int(pre[i]) - int(post[i])  # 余额下降 = 转出 SOL
        if sent > best_out:
            best_out = sent
            best_funder = k
    # 出资额需有意义（> 0.002 SOL，滤掉纯手续费噪声）
    if best_funder and best_out > 2_000_000:
        return best_funder
    return None


def _first_slot(address: str) -> int | None:
    """账户最早交易的 slot（出生 slot）。签名史 ≥1000 条视为老账户，返回 None。

    捆绑小号的 token 账户全部诞生在开盘同一个 slot（Jito 捆绑交易的铁证），
    正常散户的账户出生 slot 天然分散。
    """
    try:
        sigs = rpc.get_signatures_for_address(address, limit=1000)
    except Exception:
        return None
    if not sigs or len(sigs) >= 1000:
        return None
    try:
        return int(sigs[-1].get("slot") or 0) or None
    except Exception:
        return None


def _detect_same_slot_bundle(
    token_rows: list[dict[str, Any]],
    *,
    supply_raw: int,
) -> dict[str, Any]:
    """前 N 大非流动性 token 账户按「出生 slot」聚类。

    ≥K 个账户同一 slot 出生且合计持仓超阈值 → 判定捆绑发射（blocked）。
    与资金源聚类互补：不依赖出资路径，一跳中转/交易所出金也躲不掉。
    """
    probe = token_rows[: int(C.BUNDLE_PROBE_OWNERS)]
    slot_members: dict[int, list[str]] = {}
    slot_holdings: dict[int, int] = {}
    resolved = 0
    for row in probe:
        addr = row["address"]
        slot = _first_slot(addr)
        if not slot:
            continue
        resolved += 1
        slot_members.setdefault(slot, []).append(addr)
        slot_holdings[slot] = slot_holdings.get(slot, 0) + int(row["amount_raw"])

    result: dict[str, Any] = {"probed": len(probe), "resolved": resolved, "blocked": False}
    if not slot_members:
        return result

    top_slot, members = max(slot_members.items(), key=lambda kv: len(kv[1]))
    cluster_pct = slot_holdings[top_slot] / supply_raw if supply_raw > 0 else 1.0
    min_wallets = int(C.BUNDLE_SLOT_MIN_WALLETS)
    pct_cap = float(C.BUNDLE_SLOT_MAX_PCT)
    result.update(
        {
            "top_slot": top_slot,
            "cluster_wallets": len(members),
            "cluster_pct": round(cluster_pct, 4),
            "min_wallets": min_wallets,
            "threshold": pct_cap,
        }
    )
    if len(members) >= min_wallets and cluster_pct > pct_cap:
        result["blocked"] = True
        result["reason"] = (
            f"捆绑发射（{len(members)} 个大户 token 账户同 slot 出生，"
            f"合计仍持有供应量 {cluster_pct*100:.1f}% > {pct_cap*100:.0f}%，随时一起砸）"
        )
    return result


def _mint_owner_deltas(
    meta: dict[str, Any],
    mint: str,
    *,
    exclude: set[str],
) -> list[tuple[str, int]]:
    """从交易 meta 提取该 mint 各 owner 的净变化 (owner, delta_raw)。"""
    pre: dict[str, int] = {}
    post: dict[str, int] = {}
    for side, bag in (
        ("pre_token_balances", pre),
        ("post_token_balances", post),
    ):
        for b in meta.get(side) or []:
            if not isinstance(b, dict) or b.get("mint") != mint:
                continue
            owner = str(b.get("owner") or "")
            if not owner or owner in exclude:
                continue
            try:
                amt = int(((b.get("uiTokenAmount") or {}) or {}).get("amount") or 0)
            except Exception:
                continue
            bag[owner] = amt
    owners = set(pre) | set(post)
    out: list[tuple[str, int]] = []
    for o in owners:
        d = int(post.get(o, 0)) - int(pre.get(o, 0))
        if d != 0:
            out.append((o, d))
    return out


def _largest_equal_cluster(
    amounts: list[int],
    *,
    tol: float,
    min_wallets: int,
) -> tuple[int, int, int]:
    """返回 (cluster_size, hi, lo)。amounts 不要求预排序。"""
    vals = sorted((int(a) for a in amounts if int(a) > 0), reverse=True)
    if len(vals) < min_wallets:
        return 0, 0, 0
    best = 0
    best_hi = 0
    best_lo = 0
    n = len(vals)
    for i in range(n):
        hi = vals[i]
        if hi <= 0:
            continue
        for j in range(i + min_wallets - 1, n):
            lo = vals[j]
            if (hi - lo) / hi <= tol:
                size = j - i + 1
                if size > best:
                    best = size
                    best_hi = hi
                    best_lo = lo
            else:
                break
    return best, best_hi, best_lo


def _detect_pool_equal_size_farm(
    mint: str,
    pool: str,
    *,
    supply_raw: int,
    exclude: set[str] | None = None,
) -> dict[str, Any]:
    """池子成交等额齐动手检测（CXMT：不在前20的几百个农场号）。

    拉池子最近签名 → 优先解析「同 slot 塞满笔数」的密集窗口 →
    统计不同钱包的买/卖代币量；同 slot 或整窗内 ≥K 个钱包量几乎相等 → 拦。
    """
    pool_addr = (pool or "").strip()
    min_wallets = int(C.FARM_POOL_MIN_WALLETS)
    tol = float(C.FARM_POOL_SIZE_TOL)
    min_pct = float(C.FARM_POOL_MIN_PCT)
    max_pct = float(C.FARM_POOL_MAX_PCT)
    min_raw = int(supply_raw * min_pct) if supply_raw > 0 else 0
    max_raw = int(supply_raw * max_pct) if supply_raw > 0 else 0
    excl = set(exclude or set()) | set(_BURN_ADDRESSES)
    if pool_addr:
        excl.add(pool_addr)

    result: dict[str, Any] = {
        "pool": pool_addr[:8] + "…" if pool_addr else None,
        "min_wallets": min_wallets,
        "tol": tol,
        "blocked": False,
    }
    if not pool_addr:
        result["skipped"] = "no_pool"
        return result

    try:
        sigs = rpc.get_signatures_for_address(
            pool_addr, limit=int(C.FARM_POOL_TX_LIMIT), max_retries=2
        )
    except Exception as exc:
        result["skipped"] = f"sigs:{exc}"
        return result

    ok_sigs = [
        s
        for s in (sigs or [])
        if isinstance(s, dict) and s.get("signature") and not s.get("err")
    ]
    result["sigs"] = len(ok_sigs)
    if len(ok_sigs) < min_wallets:
        return result

    # 优先解析密集 slot（农场齐砸/齐买的铁证窗口），再补最近散单
    by_slot: dict[int, list[dict[str, Any]]] = {}
    for s in ok_sigs:
        try:
            slot = int(s.get("slot") or 0)
        except Exception:
            continue
        if slot <= 0:
            continue
        by_slot.setdefault(slot, []).append(s)

    budget = int(C.FARM_POOL_TX_PARSE)
    to_parse: list[dict[str, Any]] = []
    seen_sig: set[str] = set()
    for _slot, members in sorted(by_slot.items(), key=lambda kv: -len(kv[1])):
        if len(to_parse) >= budget:
            break
        # 同 slot ≥3 笔才值得优先挖；CXMT 崩盘 slot 有 50~100 笔
        if len(members) < 3 and to_parse:
            continue
        for s in members:
            sig = str(s["signature"])
            if sig in seen_sig:
                continue
            seen_sig.add(sig)
            to_parse.append(s)
            if len(to_parse) >= budget:
                break
    if len(to_parse) < budget:
        for s in ok_sigs:
            sig = str(s["signature"])
            if sig in seen_sig:
                continue
            seen_sig.add(sig)
            to_parse.append(s)
            if len(to_parse) >= budget:
                break

    # (slot, owner, abs_amount, side) side=buy|sell
    events: list[tuple[int, str, int, str]] = []
    parsed = 0
    for s in to_parse:
        try:
            slot = int(s.get("slot") or 0)
            meta = rpc.get_transaction_meta(str(s["signature"]))
        except Exception:
            continue
        if not meta or meta.get("err") is not None:
            continue
        parsed += 1
        try:
            deltas = _mint_owner_deltas(meta, mint, exclude=excl)
        except Exception:
            continue
        for owner, delta in deltas:
            amt = abs(int(delta))
            if amt < min_raw or amt > max_raw:
                continue
            side = "buy" if delta > 0 else "sell"
            events.append((slot, owner, amt, side))

    result["parsed"] = parsed
    result["events"] = len(events)
    if len(events) < min_wallets:
        return result

    # ① 同 slot 等额：买/卖分开看
    best_slot_hit: dict[str, Any] | None = None
    slot_groups: dict[tuple[int, str], list[tuple[str, int]]] = {}
    for slot, owner, amt, side in events:
        slot_groups.setdefault((slot, side), []).append((owner, amt))

    for (slot, side), rows in slot_groups.items():
        # 每钱包只取一笔（取最大），防同一 tx 重复计数
        by_owner: dict[str, int] = {}
        for owner, amt in rows:
            by_owner[owner] = max(by_owner.get(owner, 0), amt)
        if len(by_owner) < min_wallets:
            continue
        size, hi, lo = _largest_equal_cluster(
            list(by_owner.values()), tol=tol, min_wallets=min_wallets
        )
        if size >= min_wallets and (
            best_slot_hit is None or size > int(best_slot_hit["wallets"])
        ):
            best_slot_hit = {
                "slot": slot,
                "side": side,
                "wallets": size,
                "hi": hi,
                "lo": lo,
                "pct": round(hi / supply_raw, 6) if supply_raw > 0 else 0,
            }

    if best_slot_hit:
        result["blocked"] = True
        result["mode"] = "same_slot"
        result["hit"] = best_slot_hit
        pct = float(best_slot_hit["pct"]) * 100
        result["reason"] = (
            f"池子等额齐{'买' if best_slot_hit['side']=='buy' else '卖'}农场盘"
            f"（同 slot {best_slot_hit['slot']} 内 {best_slot_hit['wallets']} 个钱包"
            f"量相差 ≤{tol*100:.0f}%，约各占供应量 {pct:.3f}%；CXMT 类脚本分仓）"
        )
        return result

    # ② 整窗等额：跨 slot 仍有大量同量买卖（慢慢吸筹的农场）
    for side in ("buy", "sell"):
        by_owner: dict[str, int] = {}
        for _slot, owner, amt, s in events:
            if s != side:
                continue
            by_owner[owner] = max(by_owner.get(owner, 0), amt)
        if len(by_owner) < min_wallets:
            continue
        size, hi, lo = _largest_equal_cluster(
            list(by_owner.values()), tol=tol, min_wallets=min_wallets
        )
        if size >= min_wallets:
            pct = (hi / supply_raw * 100) if supply_raw > 0 else 0
            result["blocked"] = True
            result["mode"] = "window"
            result["hit"] = {
                "side": side,
                "wallets": size,
                "hi": hi,
                "lo": lo,
                "pct": round(hi / supply_raw, 6) if supply_raw > 0 else 0,
            }
            result["reason"] = (
                f"池子等额{'买' if side=='buy' else '卖'}农场盘"
                f"（近窗 {size} 个钱包量相差 ≤{tol*100:.0f}%，"
                f"约各占供应量 {pct:.3f}%；不在前20也能抓）"
            )
            return result

    return result


def _detect_bundle_clusters(
    owner_ranked: list[tuple[str, int]],
    *,
    supply_raw: int,
    exclude: set[str],
) -> dict[str, Any]:
    """对前若干控制人做资金源聚类：同一 funder 喂出的钱包合计控盘超阈值 → 拦。"""
    probe = owner_ranked[: int(C.BUNDLE_PROBE_OWNERS)]
    funder_holdings: dict[str, int] = {}
    funder_members: dict[str, list[str]] = {}
    resolved = 0
    for owner, amt in probe:
        funder = _find_funder(owner)
        if not funder or funder in exclude:
            continue
        resolved += 1
        funder_holdings[funder] = funder_holdings.get(funder, 0) + int(amt)
        funder_members.setdefault(funder, []).append(owner)

    if not funder_holdings:
        return {"resolved": 0, "blocked": False}

    top_funder, top_amt = max(funder_holdings.items(), key=lambda kv: kv[1])
    cluster_size = len(funder_members.get(top_funder, []))
    cluster_pct = top_amt / supply_raw if supply_raw > 0 else 1.0
    bundle_cap = float(C.BUNDLE_MAX_PCT)
    result = {
        "resolved": resolved,
        "top_funder": top_funder[:8] + "…",
        "cluster_wallets": cluster_size,
        "cluster_pct": round(cluster_pct, 4),
        "threshold": bundle_cap,
        "blocked": False,
    }
    # 至少 2 个钱包共享资金源且合计超阈值，才判定捆绑（单钱包已由集中度覆盖）
    if cluster_size >= 2 and cluster_pct > bundle_cap:
        result["blocked"] = True
        result["reason"] = (
            f"疑似捆绑/多钱包控盘 "
            f"({cluster_size} 个钱包同源资金，合计占供应量 {cluster_pct*100:.1f}% > {bundle_cap*100:.0f}%)"
        )
    return result


def check_holder_concentration(
    mint: str,
    *,
    pool: str | None = None,
    dex: str | None = None,
    use_cache: bool = True,
) -> HolderResult:
    """前十大非流动性持仓集中度审计。fail-closed。"""
    if use_cache:
        cached = _cache.get(mint)
        if cached and (time.time() - cached.ts) < float(C.HOLDER_CACHE_TTL_SEC):
            return cached

    checks: dict[str, Any] = {}
    reasons: list[str] = []
    whale_snapshot: dict[str, int] = {}

    try:
        try:
            supply_raw, decimals = rpc.get_mint_supply_raw(mint)
        except Exception as exc:
            reasons.append(f"链上安全检查超时/RPC错误（无法读供应量）: {exc}")
            result = HolderResult(ok=False, reasons=reasons, checks=checks)
            _cache[mint] = result
            return result

        checks["supply_raw"] = supply_raw
        checks["decimals"] = decimals

        try:
            largest = rpc.get_token_largest_accounts(mint)
        except Exception as exc:
            reasons.append(f"链上安全检查超时/RPC错误（无法拉 Holder 列表）: {exc}")
            result = HolderResult(ok=False, reasons=reasons, checks=checks)
            _cache[mint] = result
            return result

        exclude = _liquidity_token_accounts(mint, pool)
        checks["excluded_liquidity"] = sorted(exclude)[:8]
        checks["excluded_count"] = len(exclude)

        # 剔除流动性 / 烧毁后，按余额排序取前 N
        non_liq = [
            row
            for row in largest
            if row.get("address")
            and row["address"] not in exclude
            and int(row.get("amount_raw") or 0) > 0
        ]
        non_liq.sort(key=lambda r: int(r["amount_raw"]), reverse=True)

        # —— 关键：把 Token 账户解析为「控制人钱包」并按 owner 聚合 ——
        # 防止庄家用一个钱包开多个 ATA、或看板只看账户地址被绕过。
        owner_map = _resolve_owners([r["address"] for r in non_liq])
        by_owner: dict[str, int] = {}
        for r in non_liq:
            owner = owner_map.get(r["address"]) or r["address"]
            if owner in exclude:
                continue
            by_owner[owner] = by_owner.get(owner, 0) + int(r["amount_raw"])
        owner_ranked = sorted(by_owner.items(), key=lambda kv: kv[1], reverse=True)

        top_n = int(C.HOLDER_TOP_N)
        top_owners = owner_ranked[:top_n]
        checks["distinct_owners"] = len(owner_ranked)
        checks["owner_resolved"] = bool(owner_map)

        # top 用 owner 聚合结果（解析失败时回退到账户级）
        if top_owners:
            top = [{"address": o, "amount_raw": amt} for o, amt in top_owners]
        else:
            top = non_liq[:top_n]

        top_sum = sum(int(r["amount_raw"]) for r in top)
        top_pct = top_sum / supply_raw if supply_raw > 0 else 1.0
        checks["top_n"] = top_n
        checks["top_holder_count"] = len(top)
        checks["top_sum_raw"] = top_sum
        checks["top_pct"] = round(top_pct, 4)
        checks["threshold"] = float(C.HOLDER_TOP10_MAX_PCT)
        checks["top_addresses"] = [
            {"address": r["address"][:8] + "…", "pct": round(int(r["amount_raw"]) / supply_raw, 4)}
            for r in top[:5]
        ]

        # 流通盘口径（剔除流动性后）——辅佐判断老鼠仓在「可交易筹码」里的控盘度
        liq_sum = sum(
            int(r["amount_raw"])
            for r in largest
            if r.get("address") in exclude
        )
        circulating = max(0, supply_raw - liq_sum)
        circ_pct = (top_sum / circulating) if circulating > 0 else top_pct
        checks["circulating_raw"] = circulating
        checks["top_pct_of_circulating"] = round(circ_pct, 4)

        # 快照用于开仓后监控：owner → 持仓（含对应 token 账户便于复查）
        whale_snapshot = {r["address"]: int(r["amount_raw"]) for r in top}

        threshold = float(C.HOLDER_TOP10_MAX_PCT)
        if top_pct > threshold:
            reasons.append(
                f"筹码过度集中/老鼠仓控盘 "
                f"(前{top_n}大控制人持仓占供应量 {top_pct*100:.1f}% > {threshold*100:.0f}%)"
            )
        # 流通盘内极端控盘：即使总量占比未超阈值，流通盘内 > 70% 也拦
        circ_cap = float(C.HOLDER_CIRC_MAX_PCT)
        if circulating > 0 and circ_pct > circ_cap and top_pct <= threshold:
            reasons.append(
                f"筹码过度集中/老鼠仓控盘 "
                f"(前{top_n}大占流通盘 {circ_pct*100:.1f}% > {circ_cap*100:.0f}%)"
            )

        if len(top) == 0:
            # 剔除流动性后完全看不到持仓 → 无法审计，fail-closed
            reasons.append(
                "可审计非流动性持仓为空，无法确认筹码分布（未通过风控白名单）"
            )

        # —— 农场盘：池子成交等额齐动手（CXMT：几百小号不在前20）——
        if C.FARM_POOL_TX_CHECK_ENABLED and len(reasons) == 0 and pool:
            try:
                farm = _detect_pool_equal_size_farm(
                    mint, pool, supply_raw=supply_raw, exclude=exclude
                )
                checks["farm_pool_tx"] = farm
                if farm.get("blocked"):
                    reasons.append(farm["reason"])
            except Exception as exc:
                logger.warning("池子等额农场检测失败（跳过，不硬拦）%s: %s", mint[:8], exc)
                checks["farm_pool_tx"] = {"skipped": str(exc)}

        # —— 捆绑发射检测①：同 slot 出生聚类（Bubsem 验尸实锤的铁证信号）——
        # 直接用 token 账户，不依赖 owner 解析与出资路径
        if C.BUNDLE_CHECK_ENABLED and len(reasons) == 0 and non_liq:
            try:
                slot_bundle = _detect_same_slot_bundle(non_liq, supply_raw=supply_raw)
                checks["bundle_slot"] = slot_bundle
                if slot_bundle.get("blocked"):
                    reasons.append(slot_bundle["reason"])
            except Exception as exc:
                logger.warning("同slot捆绑检测失败（跳过，不硬拦）%s: %s", mint[:8], exc)
                checks["bundle_slot"] = {"skipped": str(exc)}

        # —— 捆绑发射检测②：资金源聚类（同一母钱包喂 SOL 的小号合计控盘）——
        # 仅在 owner 成功解析时才做（否则 funder 探测纯属网络浪费）
        if C.BUNDLE_CHECK_ENABLED and len(reasons) == 0 and owner_map and owner_ranked:
            try:
                bundle = _detect_bundle_clusters(
                    owner_ranked, supply_raw=supply_raw, exclude=exclude
                )
                checks["bundle"] = bundle
                if bundle.get("blocked"):
                    reasons.append(bundle["reason"])
            except Exception as exc:
                # 捆绑检测是「加分项」，其自身 RPC 失败不硬拦（否则几乎全拦）
                logger.warning("捆绑聚类检测失败（跳过，不硬拦）%s: %s", mint[:8], exc)
                checks["bundle"] = {"skipped": str(exc)}

    except Exception as exc:
        logger.exception("持仓集中度审计未预期异常 mint=%s", mint)
        reasons.append(f"筹码集中度检查异常（未通过风控白名单）: {exc}")

    result = HolderResult(
        ok=(len(reasons) == 0),
        reasons=reasons,
        checks=checks,
        whale_snapshot=whale_snapshot,
    )
    _cache[mint] = result
    return result


def detect_early_whale_dump(
    mint: str,
    *,
    snapshot: dict[str, int],
    pool: str | None = None,
) -> tuple[bool, dict[str, Any]]:
    """对比开仓时大户快照：净流出超阈值 → (True, meta)。

    快照键是「控制人钱包 owner」（与 check_holder_concentration 一致），
    不能拿 getTokenLargestAccounts 的 token 账户地址直接对拍——否则永远匹配不上，
    会被误判成 100% 砸盘（BullPad 连环误杀根因）。

    RPC 失败时返回 False（本轮跳过，下轮再试），不因一次超时误杀。
    """
    if not snapshot:
        return False, {"skip": "empty_snapshot"}

    try:
        largest = rpc.get_token_largest_accounts(mint)
    except Exception as exc:
        logger.warning("早期大户监控读 Holder 失败 %s: %s", mint[:8], exc)
        return False, {"error": str(exc), "skip": True}

    exclude = _liquidity_token_accounts(mint, pool) if pool else set(_BURN_ADDRESSES)
    token_rows = [
        row
        for row in largest
        if row.get("address")
        and row["address"] not in exclude
        and int(row.get("amount_raw") or 0) > 0
    ]
    # 与开仓快照同口径：token 账户 → owner 聚合
    owner_map = _resolve_owners([r["address"] for r in token_rows])
    current_by_owner: dict[str, int] = {}
    for r in token_rows:
        key = owner_map.get(r["address"]) or r["address"]
        if key in exclude:
            continue
        current_by_owner[key] = current_by_owner.get(key, 0) + int(r["amount_raw"])

    # 快照里的地址若解析失败（既不在 owner 聚合也不在 token 列表），才视为 0
    # —— 但若 owner_map 整批失败，禁止把「对不上」当砸盘（防误杀）
    owner_ok = bool(owner_map)
    old_total = 0
    dumped = 0
    missing = 0
    for addr, old_amt in snapshot.items():
        old_amt = int(old_amt)
        if old_amt <= 0:
            continue
        old_total += old_amt
        if addr in current_by_owner:
            new_amt = int(current_by_owner[addr])
        elif addr in {r["address"] for r in token_rows}:
            # 快照偶发是 token 账户级（旧逻辑/测试）
            new_amt = next(
                int(r["amount_raw"]) for r in token_rows if r["address"] == addr
            )
        elif not owner_ok:
            # 解析全失败：本轮跳过，避免 100% 假砸盘
            return False, {
                "skip": "owner_resolve_failed",
                "tracked": len(snapshot),
            }
        else:
            # owner 已不在 top 列表：可能真砸了，也可能跌出前 20
            # 不直接当 0，记 missing；只统计仍可见地址的减持
            missing += 1
            continue
        if new_amt < old_amt:
            dumped += old_amt - new_amt

    if old_total <= 0:
        return False, {"skip": "zero_old_total"}

    # 可见部分过少（大半跌出榜）→ 不可靠，本轮不杀
    visible_ratio = 1.0 - (missing / max(len(snapshot), 1))
    if visible_ratio < 0.5:
        return False, {
            "skip": "too_many_missing",
            "missing": missing,
            "tracked": len(snapshot),
            "visible_ratio": round(visible_ratio, 3),
        }

    dump_pct = dumped / old_total
    meta = {
        "dump_pct": round(dump_pct, 4),
        "dumped_raw": dumped,
        "old_total_raw": old_total,
        "threshold": float(C.EARLY_WHALE_DUMP_PCT),
        "tracked": len(snapshot),
        "missing": missing,
        "owner_resolved": owner_ok,
    }
    if dump_pct >= float(C.EARLY_WHALE_DUMP_PCT):
        return True, meta
    return False, meta


def clear_cache() -> None:
    _cache.clear()
