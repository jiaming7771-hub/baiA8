"""Jupiter 聚合换币：实盘买入/卖出（经 RPC 广播 + 确认超时）。"""

from __future__ import annotations

import base64
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from . import config as C
from .chain import keypair_for_live
from .rpc import RpcError, confirm_signature, redact_rpc_url, send_raw_transaction
from .risk import RiskBlocked, guard as risk_guard

logger = logging.getLogger("pumpfun.live_swap")

# 常见 meme 默认 6 decimals；可用 mint 覆盖
_DEFAULT_DECIMALS = 6


class LiveSwapError(RuntimeError):
    """实盘换币失败。"""


def _opener() -> urllib.request.OpenerDirector:
    """Jupiter 需走出境代理（PUMP_HTTP_PROXY，如 Clash）。"""
    proxy = (C.HTTP_PROXY or "").strip()
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
    return urllib.request.build_opener()


def _http_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    timeout = C.RPC_TIMEOUT_SEC if timeout is None else timeout
    data = None
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (pump-live)",
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _opener().open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")[:400]
        except Exception:
            pass
        raise LiveSwapError(f"HTTP {exc.code} {url.split('?')[0]}: {body}") from exc
    except (TimeoutError, urllib.error.URLError, OSError) as exc:
        raise LiveSwapError(f"网络超时/失败: {exc}") from exc


def get_quote(
    *,
    input_mint: str,
    output_mint: str,
    amount: int,
    slippage_bps: int,
    routing: str = "default",
    only_direct: bool | None = None,
    urgent: bool = False,
) -> dict[str, Any]:
    """Jupiter 报价。

    routing:
      - default: 常规报价（restrictIntermediateTokens）
      - graduated: 毕业迁移容灾——放开中间路径，优先走聚合 DEX（Raydium 等）
      - open: 最宽松（紧急止损最后一档）
    only_direct: 强制 onlyDirectRoutes（买入防夹时可开）
    urgent: 允许滑点突破常规 10% 硬顶（止损逃生）
    """
    bps = risk_guard.clamp_slippage_bps(slippage_bps, urgent=urgent)
    params: dict[str, Any] = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": int(amount),
        "slippageBps": int(bps),
    }
    if routing == "default":
        params["restrictIntermediateTokens"] = "true"
    elif routing == "graduated":
        # 免费档不支持 restrictIntermediateTokens=false；用放开直连限制走多跳，但仍限中间币
        params["restrictIntermediateTokens"] = "true"
        params["onlyDirectRoutes"] = "false"
    else:  # open
        # 同上：免费 Jupiter 禁止 false；尽量多跳但仍限中间币，避免 400 NOT_SUPPORTED
        params["restrictIntermediateTokens"] = "true"
        params["onlyDirectRoutes"] = "false"
        params["asLegacyTransaction"] = "false"

    if only_direct is True:
        params["onlyDirectRoutes"] = "true"
    elif only_direct is False:
        params["onlyDirectRoutes"] = "false"

    qs = urllib.parse.urlencode(params)
    url = f"{C.JUPITER_QUOTE_URL}?{qs}"
    quote = _http_json(url, method="GET")
    if not quote or "outAmount" not in quote:
        raise LiveSwapError(f"Jupiter 报价失败[{routing}]: {str(quote)[:200]}")
    return quote


def _quote_impact_pct(quote: dict[str, Any]) -> float:
    """Jupiter priceImpactPct → 小数（0.03 = 3%）。

    Jupiter 返回的是百分比字符串（"1.5" = 1.5%），统一 /100。
    """
    raw = quote.get("priceImpactPct")
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return abs(v) / 100.0


def assert_quote_vs_ref_price(
    *,
    buy_quote: dict[str, Any],
    sol_in: float,
    ref_price_sol: float,
    decimals: int = _DEFAULT_DECIMALS,
) -> dict[str, Any]:
    """Jupiter 报价均价相对确认后链上价的偏离检查。

    看板/确认价便宜、报价已经贵一截 → 取消广播，避免刚成交就浮亏。
    """
    info: dict[str, Any] = {"ref_price_sol": ref_price_sol, "sol_in": sol_in}
    if ref_price_sol <= 0 or sol_in <= 0:
        info["skipped"] = "no_ref"
        return info
    out_raw = int(buy_quote.get("outAmount") or 0)
    if out_raw <= 0:
        raise RiskBlocked("买入报价无效，无法校验相对确认价偏离")
    quote_px = sol_in / (out_raw / (10 ** max(0, int(decimals))))
    gap = (quote_px - ref_price_sol) / ref_price_sol
    info["quote_price_sol"] = quote_px
    info["gap_pct"] = round(gap * 100.0, 3)
    max_gap = float(C.ENTRY_QUOTE_MID_GAP_MAX)
    if gap > max_gap:
        raise RiskBlocked(
            f"报价相对确认价偏贵 {gap*100:.1f}% > +{max_gap*100:.0f}% "
            f"(ref={ref_price_sol:.10g} quote={quote_px:.10g}) — 取消追高"
        )
    return info


def assert_entry_liquidity(
    *,
    token_mint: str,
    buy_quote: dict[str, Any],
    slippage_bps: int,
    sol_in: float,
) -> dict[str, Any]:
    """开仓前流动性/往返审计：买得进 ≠ 卖得出；冲击过大 / 回收率过低 → 拦截。"""
    info: dict[str, Any] = {"sol_in": sol_in}
    impact = _quote_impact_pct(buy_quote)
    info["buy_impact_pct"] = round(impact, 4)
    max_impact = float(C.ENTRY_MAX_IMPACT_PCT)
    if impact > max_impact:
        raise RiskBlocked(
            f"买入冲击过大 {impact*100:.2f}% > {max_impact*100:.1f}%（盘口吃不下/易被夹）"
        )

    if not C.ROUNDTRIP_CHECK_ENABLED:
        info["roundtrip_skipped"] = True
        return info

    out_raw = int(buy_quote.get("outAmount") or 0)
    in_raw = int(buy_quote.get("inAmount") or 0)
    if out_raw <= 0 or in_raw <= 0:
        raise RiskBlocked("买入报价无效，无法做往返审计")

    # 深度倍数：按持仓 N 倍量反向卖出报价。盘口若连 N 倍都吃不下（回收率塌），
    # 说明流动性是纸糊的——我们能卖 ≠ 别人一砸不会穿（Bubsem 类抽池盘）。
    mult = max(1.0, float(C.ENTRY_ROUNDTRIP_DEPTH_MULT))
    sell_amount = int(out_raw * mult)
    try:
        sell_quote = get_quote(
            input_mint=token_mint,
            output_mint=C.SOL_MINT,
            amount=sell_amount,
            slippage_bps=slippage_bps,
            routing="default",
        )
    except LiveSwapError as exc:
        # 卖不出去 = 典型貔貅/无路由；fail-closed
        raise RiskBlocked(f"开仓前反向卖出报价失败（买得进卖不出）: {exc}") from exc

    out_sol_raw = int(sell_quote.get("outAmount") or 0)
    recovery = (out_sol_raw / (in_raw * mult)) if in_raw > 0 else 0.0
    info["depth_mult"] = mult
    sell_impact = _quote_impact_pct(sell_quote)
    info.update(
        {
            "sell_out_lamports": out_sol_raw,
            "recovery": round(recovery, 4),
            "sell_impact_pct": round(sell_impact, 4),
            "min_recovery": float(C.ROUNDTRIP_MIN_RECOVERY),
        }
    )
    min_rec = float(C.ROUNDTRIP_MIN_RECOVERY)
    if recovery < min_rec:
        raise RiskBlocked(
            f"往返回收率过低 {recovery*100:.1f}% < {min_rec*100:.0f}% "
            f"（买{sol_in:.4f}SOL → 反手仅得{out_sol_raw/1e9:.4f}SOL，疑似税/薄流动性）"
        )
    return info


# Pump 毕业 / 曲线失效常见错误特征（大小写不敏感）
_GRADUATION_ERR_MARKERS = (
    "no routes found",
    "no route",
    "could not find any route",
    "could not find",
    "insufficient liquidity",
    "route not found",
    "unable to find a route",
    "token_not_tradable",
    "not tradable",
    "bonding curve",
    "curve complete",
    "curve is complete",
    "migrated",
    "graduation",
    "accountnotinitialized",
    "invalid amm account",
    "pool not found",
)

# PumpSwap 创作者费升级后：交易执行时引用了未初始化的
# coin_creator_vault_ata / pool_v2 等账户 → sendTransaction 模拟报
# "MissingAccount" / "account required by the instruction is missing"。
# 这类失败换一次新报价（新路由 + 新区块哈希，且该 ATA 常被他人的交易顺带建好）后往往即可成交。
_MISSING_ACCOUNT_ERR_MARKERS = (
    "missingaccount",
    "an account required by the instruction is missing",
    "instruction references an unknown account",
    "unknown account",
    "blockhashnotfound",
    "block height exceeded",
    "blockhash not found",
)

# Jupiter / PumpSwap：报价到广播之间盘口一动就会超滑点。
# 同一签名再打没用，必须重新 getQuote（不抬 slip）后再签。
_SLIPPAGE_ERR_MARKERS = (
    "0x1771",
    "custom': 6001",
    'custom": 6001',
    "custom program error: 0x1771",
    "slippagetoleranceexceeded",
    "slippage tolerance exceeded",
    "exceeded slippage",
    "slippage exceeded",
)


def looks_like_graduation_or_route_failure(exc: BaseException | str) -> bool:
    """判断是否像「泵毕业 / 曲线失效 / 无流动性」类路由错误。"""
    text = str(exc).lower()
    return any(m.lower() in text for m in _GRADUATION_ERR_MARKERS)


def looks_like_missing_account_failure(exc: BaseException | str) -> bool:
    """判断是否像 PumpSwap 创作者费/账户缺失类失败（重新报价后可重试）。"""
    text = str(exc).lower()
    return any(m in text for m in _MISSING_ACCOUNT_ERR_MARKERS)


def looks_like_slippage_failure(exc: BaseException | str) -> bool:
    """判断是否像滑点超限（须重报价，不可复用同一笔签名）。"""
    text = str(exc).lower()
    return any(m in text for m in _SLIPPAGE_ERR_MARKERS)


def _log_alert_to_journal(
    *,
    action: str,
    message: str,
    mint: str = "",
    symbol: str = "",
    amount_sol: float = 0.0,
    context: dict[str, Any] | None = None,
) -> None:
    try:
        from . import journal

        journal.record_alert(
            action=action,
            message=message,
            mint=mint,
            symbol=symbol,
            amount_sol=amount_sol,
            context=context,
            dry_run=False,
            shadow=False,
        )
    except Exception:
        logger.exception("写入告警到 trades.jsonl 失败")


def assert_wallet_rent_safe_for_buy(
    *,
    owner: str,
    buy_sol: float,
    mint: str = "",
) -> dict[str, Any]:
    """买入前：链上 SOL 必须覆盖 买入额 + ATA 租金 + Gas 底仓，且总余额 ≥ 地板。

    防止创建 ATA 把钱包打干 → 后续无法卖出/止损。
    """
    from .rpc import get_balance_sol

    try:
        bal = float(get_balance_sol(owner))
    except Exception as exc:
        msg = f"读取钱包余额失败，拒绝开仓: {exc}"
        logger.error("🚨 %s", msg)
        _log_alert_to_journal(
            action="rent_block",
            message=msg,
            mint=mint,
            amount_sol=buy_sol,
            context={"error": str(exc)},
        )
        raise RiskBlocked(msg) from exc

    ata_rent = float(C.ATA_RENT_SOL)
    reserve = float(C.WALLET_RESERVE_SOL)
    floor = float(C.WALLET_MIN_SOL_FLOOR)
    buy = float(buy_sol)

    # 已有 ATA 则不必再扣租金（仍保留底仓）
    need_ata_rent = True
    try:
        if mint:
            from .rpc import rpc_call

            result = rpc_call(
                "getTokenAccountsByOwner",
                [owner, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}],
                max_retries=1,
                timeout=min(8.0, C.RPC_TIMEOUT_SEC),
            )
            accounts = (result or {}).get("value") or []
            if accounts:
                need_ata_rent = False
    except Exception as exc:
        logger.warning("ATA 存在性检查失败，按需新建租金预留: %s", exc)
        need_ata_rent = True

    rent_budget = ata_rent if need_ata_rent else 0.0
    # 买入后剩余 = bal - buy - rent；还必须 ≥ reserve，且买入前总余额 ≥ floor
    remaining_after = bal - buy - rent_budget
    info = {
        "wallet_sol": round(bal, 9),
        "buy_sol": round(buy, 9),
        "ata_rent_sol": round(rent_budget, 9),
        "need_ata": need_ata_rent,
        "reserve_sol": round(reserve, 9),
        "floor_sol": round(floor, 9),
        "remaining_after": round(remaining_after, 9),
    }

    if bal + 1e-12 < floor:
        msg = (
            f"钱包 SOL 低于安全地板 {floor:.4f}（当前 {bal:.6f}）— 拒绝开仓，"
            f"防止余额打干后无法止损"
        )
        logger.error("🚨 %s | %s", msg, info)
        _log_alert_to_journal(
            action="rent_block",
            message=msg,
            mint=mint,
            amount_sol=buy,
            context=info,
        )
        raise RiskBlocked(msg)

    if remaining_after + 1e-12 < reserve:
        msg = (
            f"买入后 SOL 不足覆盖 ATA租金+底仓：余额={bal:.6f} 买入={buy:.4f} "
            f"租金={rent_budget:.6f} 买入后剩余={remaining_after:.6f} < 底仓={reserve:.4f}"
        )
        logger.error("🚨 %s | %s", msg, info)
        _log_alert_to_journal(
            action="rent_block",
            message=msg,
            mint=mint,
            amount_sol=buy,
            context=info,
        )
        raise RiskBlocked(msg)

    # 额外：买入额本身 + 10% gas 垫付（与 risk 现金 90% 规则对齐的显式检查）
    gas_pad = buy * 0.10
    if bal + 1e-12 < buy + rent_budget + max(reserve, gas_pad):
        msg = (
            f"余额不足以支付 买入+租金+Gas垫付：需≥{buy + rent_budget + max(reserve, gas_pad):.6f} "
            f"当前={bal:.6f}"
        )
        logger.error("🚨 %s | %s", msg, info)
        _log_alert_to_journal(
            action="rent_block",
            message=msg,
            mint=mint,
            amount_sol=buy,
            context={**info, "gas_pad": gas_pad},
        )
        raise RiskBlocked(msg)

    logger.info(
        "✅ 租金安全检查通过 wallet=%.6f buy=%.4f ata_rent=%.6f need_ata=%s remaining=%.6f",
        bal,
        buy,
        rent_budget,
        need_ata_rent,
        remaining_after,
    )
    return info


def _priority_fee_field() -> Any:
    """优先费策略：Jito tip > priorityLevel+maxLamports > auto。

    目的：拥堵/剧烈波动时，硬止损与时间止损必须能优先上链，
    绝不允许卡在 Mempool 无法止损。
    """
    if int(C.JITO_TIP_LAMPORTS) > 0:
        return {"jitoTipLamports": int(C.JITO_TIP_LAMPORTS)}
    if int(C.PRIORITY_FEE_MAX_LAMPORTS) > 0:
        return {
            "priorityLevelWithMaxLamports": {
                "maxLamports": int(C.PRIORITY_FEE_MAX_LAMPORTS),
                "priorityLevel": C.PRIORITY_LEVEL or "veryHigh",
            }
        }
    return "auto"


def build_swap_tx(quote: dict[str, Any], user_pubkey: str) -> bytes:
    body = {
        "quoteResponse": quote,
        "userPublicKey": user_pubkey,
        "wrapAndUnwrapSol": True,
        "dynamicComputeUnitLimit": True,
        "prioritizationFeeLamports": _priority_fee_field(),
    }
    resp = _http_json(C.JUPITER_SWAP_URL, method="POST", payload=body)
    b64 = resp.get("swapTransaction")
    if not b64:
        raise LiveSwapError(f"Jupiter swap 无交易体: {str(resp)[:200]}")
    return base64.b64decode(b64)


def fetch_actual_fill(
    *,
    signature: str,
    owner: str,
    mint: str,
    side: str,
    decimals: int = _DEFAULT_DECIMALS,
    quote_price: float = 0.0,
) -> dict[str, Any]:
    """确认后回读链上真实成交：实际均价 / 真实滑点差值 / 实际 Gas。

    best-effort：RPC 拉不到明细时返回 {"ok": False}，不阻塞主流程。
    """
    from .rpc import get_transaction_meta

    try:
        meta = get_transaction_meta(signature)
    except Exception as exc:
        logger.warning("回读成交明细失败 sig=%s…: %s", signature[:8], exc)
        return {"ok": False, "error": str(exc)}
    if not meta or meta.get("err") is not None:
        return {"ok": False, "error": f"tx err={None if not meta else meta.get('err')}"}

    fee_sol = meta["fee_lamports"] / float(C.LAMPORTS_PER_SOL)

    # owner 的 SOL 变化（fee payer 通常是 index 0，稳妥起见按 account_keys 找）
    sol_delta = 0.0
    keys = meta.get("account_keys") or []
    try:
        idx = keys.index(owner) if owner in keys else 0
        pre = meta["pre_balances"][idx]
        post = meta["post_balances"][idx]
        sol_delta = (post - pre) / float(C.LAMPORTS_PER_SOL)
    except Exception:
        pass

    # owner 的目标 Token 变化
    def _tok_amt(rows: list) -> int:
        total = 0
        for r in rows:
            try:
                if r.get("mint") == mint and r.get("owner") == owner:
                    total += int(r["uiTokenAmount"]["amount"])
            except Exception:
                continue
        return total

    tok_delta_raw = _tok_amt(meta["post_token_balances"]) - _tok_amt(meta["pre_token_balances"])
    tok_delta = tok_delta_raw / (10 ** max(0, int(decimals)))

    fill_price_actual = 0.0
    if abs(tok_delta) > 1e-18:
        if side == "buy":
            # 买入：SOL 净流出（含 fee）；成交均价剔除 gas
            fill_price_actual = max(0.0, (-sol_delta - fee_sol)) / abs(tok_delta)
        else:
            fill_price_actual = max(0.0, (sol_delta + fee_sol)) / abs(tok_delta)

    slip_real_pct = None
    if quote_price > 0 and fill_price_actual > 0:
        # buy：实际价高于报价为负滑（吃亏）；sell：实际价低于报价为负滑
        if side == "buy":
            slip_real_pct = (fill_price_actual - quote_price) / quote_price * 100.0
        else:
            slip_real_pct = (quote_price - fill_price_actual) / quote_price * 100.0

    out = {
        "ok": True,
        "signature": signature,
        "fee_sol": round(fee_sol, 9),
        "sol_delta": round(sol_delta, 9),
        "token_delta_raw": tok_delta_raw,
        "token_delta": tok_delta,
        "fill_price_actual": fill_price_actual,
        "quote_price": quote_price,
        "slippage_real_pct": None if slip_real_pct is None else round(slip_real_pct, 4),
    }
    logger.info(
        "🧾 链上成交审计 side=%s sig=%s…%s 实际均价=%.10g 报价=%.10g 真实滑点=%s%% gas=%.9f SOL",
        side,
        signature[:8],
        signature[-6:],
        fill_price_actual,
        quote_price,
        "?" if slip_real_pct is None else f"{slip_real_pct:+.3f}",
        fee_sol,
    )
    return out


def sign_versioned_tx(raw_tx: bytes) -> bytes:
    """用实盘钱包重签 VersionedTransaction。"""
    from solders.transaction import VersionedTransaction

    kp = keypair_for_live()
    tx = VersionedTransaction.from_bytes(raw_tx)
    signed = VersionedTransaction(tx.message, [kp])
    return bytes(signed)


def send_and_confirm(signed_tx: bytes) -> dict[str, Any]:
    t0 = time.time()
    try:
        sig = send_raw_transaction(signed_tx)
    except RpcError as exc:
        raise LiveSwapError(f"广播失败: {exc}") from exc
    logger.info(
        "TX sent sig=%s…%s rpc=%s — 等待确认(≤%.0fs)",
        sig[:8],
        sig[-6:],
        redact_rpc_url(),
        C.TX_CONFIRM_TIMEOUT_SEC,
    )
    try:
        status = confirm_signature(sig)
    except RpcError as exc:
        # 超时或失败：明确告警，避免无限 Pending
        logger.error("🚨 交易确认异常 sig=%s…%s: %s", sig[:8], sig[-6:], exc)
        raise LiveSwapError(str(exc)) from exc
    return {
        "signature": sig,
        "status": status,
        "elapsed_sec": round(time.time() - t0, 2),
    }


def assert_pre_send_price_ok(
    *,
    token_mint: str,
    ref_price_sol: float,
    pool: str | None = None,
    dex: str | None = None,
) -> dict[str, Any]:
    """广播前再读链上价：确认→广播之间若已追高，取消发送。

    CXMT 类问题的第二道闸：报价通过后到签名广播还有几百毫秒到数秒，
    盘口继续拉升时必须能停手。
    """
    info: dict[str, Any] = {"ref_price_sol": ref_price_sol}
    if ref_price_sol <= 0:
        info["skipped"] = "no_ref"
        return info
    try:
        from .onchain_price import fetch_pool_price_sol

        meta = fetch_pool_price_sol(token_mint, pool=pool, dex=dex)
    except Exception as exc:
        # 读不到不硬拦（否则 RPC 抖一下就全停）；交给报价偏离闸
        info["skipped"] = f"onchain_error:{exc}"
        return info
    chain_px = float((meta or {}).get("price") or 0)
    info["chain_price_sol"] = chain_px
    if chain_px <= 0:
        info["skipped"] = "no_chain_price"
        return info
    rise = (chain_px - ref_price_sol) / ref_price_sol
    info["rise_pct"] = round(rise * 100.0, 3)
    max_rise = float(C.ENTRY_PRE_SEND_RISE_MAX)
    if rise > max_rise:
        raise RiskBlocked(
            f"广播前链上价已相对确认价 +{rise*100:.1f}% > +{max_rise*100:.0f}% "
            f"(ref={ref_price_sol:.10g} chain={chain_px:.10g}) — 取消追高"
        )
    return info


def buy_token_with_sol(
    *,
    token_mint: str,
    sol_amount: float,
    slippage_bps: int | None = None,
    equity: float,
    cash: float,
    stop_file: bool = False,
    ref_price_sol: float | None = None,
    pool: str | None = None,
    dex: str | None = None,
) -> dict[str, Any]:
    """SOL → Token。开仓前强制风控 + 租金/底仓保护。

    ref_price_sol：确认后的链上参考价；用于拦截「报价已比决策价贵很多」。
    """
    if slippage_bps is None:
        slippage_bps = int(C.ENTRY_MAX_SLIPPAGE_BPS)
    gate = risk_guard.pre_trade_gate(
        side="buy",
        equity=equity,
        cash=cash,
        amount_sol=sol_amount,
        slippage_bps=slippage_bps,
        stop_file=stop_file,
    )
    sol = float(gate["amount_sol"])
    bps = int(gate["slippage_bps"])
    lamports = int(round(sol * C.LAMPORTS_PER_SOL))
    if lamports <= 0:
        raise RiskBlocked("买入 lamports 无效")

    kp = keypair_for_live()
    pubkey = str(kp.pubkey())

    # —— 租金 / 底仓前置校验（链上真实余额）——
    rent_info = assert_wallet_rent_safe_for_buy(owner=pubkey, buy_sol=sol, mint=token_mint)

    def _buy_quote(routing: str, only_direct: bool | None) -> dict[str, Any]:
        return get_quote(
            input_mint=C.SOL_MINT,
            output_mint=token_mint,
            amount=lamports,
            slippage_bps=bps,
            routing=routing,
            only_direct=only_direct,
        )

    quote = None
    routing_used = "default"
    try_direct = bool(C.ENTRY_PREFER_DIRECT_ROUTES)
    try:
        quote = _buy_quote("default", True if try_direct else None)
        routing_used = "default_direct" if try_direct else "default"
    except LiveSwapError as exc:
        # 直连失败 → 回退非直连 default
        if try_direct:
            logger.info("买入直连路由失败，回退聚合: %s", exc)
            try:
                quote = get_quote(
                    input_mint=C.SOL_MINT,
                    output_mint=token_mint,
                    amount=lamports,
                    slippage_bps=bps,
                    routing="default",
                    only_direct=False,
                )
                routing_used = "default"
            except LiveSwapError as exc2:
                exc = exc2
                quote = None
        if quote is None:
            if looks_like_graduation_or_route_failure(exc):
                logger.warning("🚨 买入默认路由失败，切换聚合路由重试: %s", exc)
                _log_alert_to_journal(
                    action="route_failover",
                    message=f"买入路由切换 graduated: {exc}",
                    mint=token_mint,
                    amount_sol=sol,
                    context={"phase": "buy_quote", "from": "default", "to": "graduated"},
                )
                quote = get_quote(
                    input_mint=C.SOL_MINT,
                    output_mint=token_mint,
                    amount=lamports,
                    slippage_bps=bps,
                    routing="graduated",
                )
                routing_used = "graduated"
            else:
                _log_alert_to_journal(
                    action="swap_error",
                    message=str(exc),
                    mint=token_mint,
                    amount_sol=sol,
                    context={"phase": "buy_quote"},
                )
                raise

    # —— 开仓前冲击 + 往返卖出审计（能买进 ≠ 能卖出）——
    try:
        rt_info = assert_entry_liquidity(
            token_mint=token_mint,
            buy_quote=quote,
            slippage_bps=bps,
            sol_in=sol,
        )
        rt_info["routing"] = routing_used
        if ref_price_sol is not None and float(ref_price_sol) > 0:
            gap_info = assert_quote_vs_ref_price(
                buy_quote=quote,
                sol_in=sol,
                ref_price_sol=float(ref_price_sol),
            )
            rt_info["quote_vs_ref"] = gap_info
    except RiskBlocked as exc:
        logger.error("🚨 开仓前流动性/往返拦截 %s…: %s", token_mint[:6], exc)
        _log_alert_to_journal(
            action="roundtrip_block",
            message=str(exc),
            mint=token_mint,
            amount_sol=sol,
            context={"phase": "entry_liquidity", "routing": routing_used},
        )
        raise

    # —— 广播前再验链上价：报价通过后到签名之间若已追高，停手 ——
    if ref_price_sol is not None and float(ref_price_sol) > 0:
        try:
            pre_send = assert_pre_send_price_ok(
                token_mint=token_mint,
                ref_price_sol=float(ref_price_sol),
                pool=pool,
                dex=dex,
            )
            rt_info["pre_send"] = pre_send
        except RiskBlocked as exc:
            logger.error("🚨 广播前追高拦截 %s…: %s", token_mint[:6], exc)
            _log_alert_to_journal(
                action="pre_send_block",
                message=str(exc),
                mint=token_mint,
                amount_sol=sol,
                context={"phase": "pre_send", "routing": routing_used},
            )
            raise

    # —— 广播：失败时重新报价重试
    # MissingAccount / 路由失效 → 换聚合；滑点 0x1771 → 同路由立刻重报价（不抬 bps）——
    max_send_attempts = 1 + max(0, int(C.BUY_SEND_MAX_RETRIES))
    conf = None
    last_exc: Exception | None = None
    for attempt in range(1, max_send_attempts + 1):
        try:
            # 重试轮也再验一次：换路由重新报价后盘口可能又拉了
            if (
                attempt > 1
                and ref_price_sol is not None
                and float(ref_price_sol) > 0
            ):
                pre_send = assert_pre_send_price_ok(
                    token_mint=token_mint,
                    ref_price_sol=float(ref_price_sol),
                    pool=pool,
                    dex=dex,
                )
                rt_info["pre_send"] = pre_send
            raw_tx = build_swap_tx(quote, pubkey)
            signed = sign_versioned_tx(raw_tx)
            conf = send_and_confirm(signed)
            break
        except RiskBlocked:
            raise
        except (LiveSwapError, RpcError) as exc:
            last_exc = exc
            missing = looks_like_missing_account_failure(exc)
            route_fail = looks_like_graduation_or_route_failure(exc)
            slip_fail = looks_like_slippage_failure(exc)
            retryable = missing or route_fail or slip_fail
            logger.error(
                "🚨 买入广播失败 attempt=%d/%d mint=%s… route=%s "
                "missing_acct=%s slip=%s: %s",
                attempt,
                max_send_attempts,
                token_mint[:6],
                routing_used,
                missing,
                slip_fail,
                exc,
            )
            if attempt >= max_send_attempts or not retryable:
                _log_alert_to_journal(
                    action="swap_error",
                    message=str(exc),
                    mint=token_mint,
                    amount_sol=sol,
                    context={
                        "phase": "buy_send",
                        "attempt": attempt,
                        "routing": routing_used,
                        "missing_account": missing,
                        "slippage": slip_fail,
                        "rent_check": rent_info,
                    },
                )
                raise LiveSwapError(f"买入失败: {exc}") from exc

            # 滑点：同路由立刻重报价（不抬 bps）；账户/路由问题：换聚合路由。
            if slip_fail and not (missing or route_fail):
                next_routing = routing_used
                only_direct: bool | None
                if routing_used == "default_direct":
                    quote_routing, only_direct = "default", True
                elif routing_used == "default":
                    quote_routing, only_direct = "default", False
                else:
                    quote_routing, only_direct = routing_used, False
                _log_alert_to_journal(
                    action="slip_requote",
                    message=f"买入滑点重报价 {routing_used} attempt={attempt}: {exc}",
                    mint=token_mint,
                    amount_sol=sol,
                    context={
                        "phase": "buy_send_slip_requote",
                        "attempt": attempt,
                        "routing": routing_used,
                    },
                )
                time.sleep(min(0.2 * attempt, 0.6))
            else:
                next_routing = (
                    "graduated" if routing_used.startswith("default") else "open"
                )
                quote_routing, only_direct = next_routing, False
                _log_alert_to_journal(
                    action="route_failover",
                    message=f"买入广播重试 {routing_used} → {next_routing}: {exc}",
                    mint=token_mint,
                    amount_sol=sol,
                    context={
                        "phase": "buy_send_retry",
                        "attempt": attempt,
                        "missing_account": missing,
                    },
                )
                time.sleep(min(1.0 * attempt, 3.0))

            try:
                quote = _buy_quote(quote_routing, only_direct)
                routing_used = next_routing
                rt_info["routing"] = routing_used
                if ref_price_sol is not None and float(ref_price_sol) > 0:
                    gap_info = assert_quote_vs_ref_price(
                        buy_quote=quote,
                        sol_in=sol,
                        ref_price_sol=float(ref_price_sol),
                    )
                    rt_info["quote_vs_ref"] = gap_info
            except RiskBlocked as exc2:
                logger.error("买入重试报价偏离拦截: %s", exc2)
                raise
            except LiveSwapError as exc2:
                # 重新报价失败：保留旧 quote 再试一次（可能只是瞬时无路由）
                logger.warning("买入重试重新报价失败（沿用上次报价）: %s", exc2)

    if conf is None:
        raise LiveSwapError(f"买入失败: {last_exc}")

    out_amount = int(quote.get("outAmount") or 0)
    quote_price = (sol / (out_amount / (10 ** _DEFAULT_DECIMALS))) if out_amount else 0.0

    # 确认后回读链上真实成交（实际均价/真实滑点/实际 gas），并以链上余额校正持仓数量
    actual = fetch_actual_fill(
        signature=conf["signature"],
        owner=pubkey,
        mint=token_mint,
        side="buy",
        decimals=_DEFAULT_DECIMALS,
        quote_price=quote_price,
    )
    filled_raw = out_amount
    fill_price = quote_price
    if actual.get("ok") and actual.get("token_delta_raw"):
        filled_raw = int(actual["token_delta_raw"])
        if actual.get("fill_price_actual"):
            fill_price = float(actual["fill_price_actual"])

    logger.info(
        "[LIVE] BUY mint=%s… sol=%.6f out_raw=%s fill=%.10g sig=%s",
        token_mint[:6],
        sol,
        filled_raw,
        fill_price,
        conf["signature"][:12],
    )
    return {
        "side": "buy",
        "mint": token_mint,
        "sol_amount": sol,
        "out_amount_raw": filled_raw,
        "decimals": _DEFAULT_DECIMALS,
        "qty": filled_raw / (10 ** _DEFAULT_DECIMALS) if filled_raw else 0.0,
        "fill_price": fill_price,
        "quote_price": quote_price,
        "ref_price_sol": float(ref_price_sol) if ref_price_sol else None,
        "slippage_bps": bps,
        "slippage_real_pct": actual.get("slippage_real_pct"),
        "gas_sol": actual.get("fee_sol") or 0.0,
        "signature": conf["signature"],
        "elapsed_sec": conf["elapsed_sec"],
        "rent_check": rent_info,
        "entry_liquidity": rt_info,
        "quote": {
            "inAmount": quote.get("inAmount"),
            "outAmount": quote.get("outAmount"),
            "priceImpactPct": quote.get("priceImpactPct"),
            "routePlan": quote.get("routePlan"),
        },
    }


class LiquidityCollapse(LiveSwapError):
    """报价可兑现的 SOL 远低于盘口估值：池子被抽干 / 盘口价失真。"""


def _sell_once(
    *,
    token_mint: str,
    token_amount_raw: int,
    decimals: int,
    bps: int,
    pubkey: str,
    routing: str = "default",
    expect_sol: float = 0.0,
    force: bool = False,
    urgent: bool = False,
) -> dict[str, Any]:
    """单次卖出尝试：报价 → 兑现校验 → 签名 → 广播 → 确认 → 真实成交审计。"""
    try:
        quote = get_quote(
            input_mint=token_mint,
            output_mint=C.SOL_MINT,
            amount=int(token_amount_raw),
            slippage_bps=bps,
            routing=routing,
            urgent=urgent or force,
        )
    except LiveSwapError:
        raise
    except Exception as exc:
        raise LiveSwapError(f"卖出报价异常[{routing}]: {exc}") from exc

    out_lamports = int(quote.get("outAmount") or 0)

    # 报价兑现校验：盘口价可能因抽池而失真，能换回多少 SOL 才是真的
    quote_sol = out_lamports / float(C.LAMPORTS_PER_SOL)
    if expect_sol > 0:
        floor_sol = expect_sol * (1.0 - float(C.EXIT_MAX_IMPACT_PCT))
        if quote_sol < floor_sol:
            shortfall = 1.0 - (quote_sol / expect_sol if expect_sol else 0.0)
            msg = (
                f"报价仅可兑现 {quote_sol:.6f} SOL，盘口估值 {expect_sol:.6f} SOL"
                f"（缩水 {shortfall * 100:.1f}% > {C.EXIT_MAX_IMPACT_PCT * 100:.0f}%）"
            )
            if not force:
                raise LiquidityCollapse(msg)
            logger.error("🚨 流动性坍塌但保命单强制卖出：%s", msg)
    try:
        raw_tx = build_swap_tx(quote, pubkey)
        signed = sign_versioned_tx(raw_tx)
        conf = send_and_confirm(signed)
    except (LiveSwapError, RpcError) as exc:
        raise
    except Exception as exc:
        raise LiveSwapError(f"卖出广播异常[{routing}]: {exc}") from exc

    sol_out = out_lamports / float(C.LAMPORTS_PER_SOL)
    qty = token_amount_raw / (10 ** max(0, int(decimals)))
    quote_price = (sol_out / qty) if qty > 0 else 0.0

    actual: dict[str, Any] = {"ok": False}
    try:
        actual = fetch_actual_fill(
            signature=conf["signature"],
            owner=pubkey,
            mint=token_mint,
            side="sell",
            decimals=decimals,
            quote_price=quote_price,
        )
    except Exception as exc:
        logger.warning("🚨 卖出成交审计失败（不影响成交）: %s", exc)

    fill_price = quote_price
    if actual.get("ok"):
        if actual.get("fill_price_actual"):
            fill_price = float(actual["fill_price_actual"])
        # 卖出真实 SOL 回款 = owner SOL 增量 + gas
        if actual.get("sol_delta") is not None:
            real_out = float(actual["sol_delta"]) + float(actual.get("fee_sol") or 0)
            if real_out > 0:
                sol_out = real_out

    logger.info(
        "[LIVE] SELL mint=%s… sol_out=%.6f fill=%.10g slip_bps=%d route=%s sig=%s",
        token_mint[:6],
        sol_out,
        fill_price,
        bps,
        routing,
        conf["signature"][:12],
    )
    return {
        "side": "sell",
        "mint": token_mint,
        "sol_amount": sol_out,
        "in_amount_raw": token_amount_raw,
        "decimals": decimals,
        "qty": qty,
        "fill_price": fill_price,
        "quote_price": quote_price,
        "slippage_bps": bps,
        "slippage_real_pct": actual.get("slippage_real_pct"),
        "gas_sol": actual.get("fee_sol") or 0.0,
        "signature": conf["signature"],
        "elapsed_sec": conf["elapsed_sec"],
        "routing": routing,
        "quote": {
            "inAmount": quote.get("inAmount"),
            "outAmount": quote.get("outAmount"),
            "priceImpactPct": quote.get("priceImpactPct"),
            "routePlan": quote.get("routePlan"),
        },
    }


def sell_token_for_sol(
    *,
    token_mint: str,
    token_amount_raw: int,
    decimals: int = _DEFAULT_DECIMALS,
    slippage_bps: int | None = None,
    equity: float,
    approx_sol: float = 0.0,
    urgent: bool = False,
) -> dict[str, Any]:
    """Token → SOL。平仓不受开仓熔断阻止。

    urgent=True（硬止损/时间止损/死盘早砍等保命单）：
    1) 失败后抬滑点重试
    2) 若错误像「泵毕业/曲线失效/无流动性」→ 自动切换 graduated/open 聚合路由
       （Raydium / Jupiter 全路径），确保不会因毕业迁池导致无法割肉。

    非 urgent 默认也走路由梯队 + 有限重试（MissingAccount 常见）；
    全路由坍塌且 EXIT_FORCE_SALVAGE 开启时强制 salvage，避免 write_off=0。
    """
    if token_amount_raw <= 0:
        raise RiskBlocked("卖出数量无效")
    gate = risk_guard.pre_trade_gate(
        side="sell",
        equity=equity,
        cash=max(equity, 0.0),
        amount_sol=max(approx_sol, 1e-9),
        slippage_bps=slippage_bps,
        stop_file=False,
    )
    bps = int(gate["slippage_bps"])
    kp = keypair_for_live()
    pubkey = str(kp.pubkey())

    retries = max(0, int(C.EXIT_SELL_MAX_RETRIES))
    if urgent or C.EXIT_SELL_RETRY_NON_URGENT:
        max_attempts = 1 + retries
    else:
        max_attempts = 1
    # 路由梯队：默认 → 毕业聚合 → 最宽松开放
    route_ladder = ["default", "graduated", "open"]
    routing = "default"
    last_err: Exception | None = None

    collapsed_routes: set[str] = set()

    def _force_salvage(why: str) -> dict[str, Any]:
        logger.error("🚨 全路由流动性坍塌，强制 salvage 成交：%s", why)
        return _sell_once(
            token_mint=token_mint,
            token_amount_raw=token_amount_raw,
            decimals=decimals,
            bps=risk_guard.clamp_slippage_bps(
                C.URGENT_SLIPPAGE_BPS_MAX, urgent=True
            ),
            pubkey=pubkey,
            routing=route_ladder[-1],
            expect_sol=approx_sol,
            force=True,
            urgent=True,
        )

    for attempt in range(1, max_attempts + 1):
        try:
            return _sell_once(
                token_mint=token_mint,
                token_amount_raw=token_amount_raw,
                decimals=decimals,
                bps=bps,
                pubkey=pubkey,
                routing=routing,
                expect_sol=approx_sol,
                urgent=urgent,
            )
        except LiquidityCollapse as exc:
            last_err = exc
            collapsed_routes.add(routing)
            logger.error(
                "🚨 流动性坍塌 mint=%s… route=%s urgent=%s: %s",
                token_mint[:6],
                routing,
                urgent,
                exc,
            )
            _log_alert_to_journal(
                action="liquidity_collapse",
                message=str(exc),
                mint=token_mint,
                amount_sol=approx_sol,
                context={
                    "phase": "sell",
                    "routing": routing,
                    "urgent": urgent,
                    "attempt": attempt,
                },
            )
            # 换路由再探：可能只是当前路由的池子被抽干
            remaining = [r for r in route_ladder if r not in collapsed_routes]
            if remaining:
                routing = remaining[0]
                continue
            if urgent or C.EXIT_FORCE_SALVAGE:
                # 保命单 / 强制逃生：所有路由都坍塌 → 强制卖出止血，能收多少收多少
                return _force_salvage(str(exc))
            # 显式关闭 salvage：放弃卖出，别按假价砸盘
            raise
        except (LiveSwapError, RpcError) as exc:
            last_err = exc
            graduated = looks_like_graduation_or_route_failure(
                exc
            ) or looks_like_missing_account_failure(exc)
            logger.error(
                "🚨 卖出失败 attempt=%d/%d mint=%s… slip=%dbps route=%s graduated_hint=%s: %s",
                attempt,
                max_attempts,
                token_mint[:6],
                bps,
                routing,
                graduated,
                exc,
            )
            _log_alert_to_journal(
                action="swap_error",
                message=str(exc),
                mint=token_mint,
                amount_sol=approx_sol,
                context={
                    "phase": "sell",
                    "attempt": attempt,
                    "routing": routing,
                    "urgent": urgent,
                    "graduation_hint": graduated,
                    "slippage_bps": bps,
                },
            )

            # 毕业/无流动性/MissingAccount → 立即切下一档路由
            if graduated:
                try:
                    idx = route_ladder.index(routing)
                except ValueError:
                    idx = 0
                if idx + 1 < len(route_ladder):
                    nxt = route_ladder[idx + 1]
                    logger.warning(
                        "🚨 检测到泵毕业/路由失效 → 切换 %s → %s 聚合重试",
                        routing,
                        nxt,
                    )
                    _log_alert_to_journal(
                        action="route_failover",
                        message=f"卖出路由切换 {routing} → {nxt}: {exc}",
                        mint=token_mint,
                        amount_sol=approx_sol,
                        context={
                            "from": routing,
                            "to": nxt,
                            "urgent": urgent,
                            "error": str(exc)[:300],
                        },
                    )
                    routing = nxt
                    # 毕业切换不消耗滑点升级额度时，额外给一次同 attempt 立即重试
                    try:
                        return _sell_once(
                            token_mint=token_mint,
                            token_amount_raw=token_amount_raw,
                            decimals=decimals,
                            bps=bps,
                            pubkey=pubkey,
                            routing=routing,
                            expect_sol=approx_sol,
                            urgent=urgent,
                        )
                    except LiquidityCollapse as exc_lc:
                        last_err = exc_lc
                        collapsed_routes.add(routing)
                        logger.error("🚨 聚合路由 %s 流动性坍塌: %s", routing, exc_lc)
                    except (LiveSwapError, RpcError) as exc2:
                        last_err = exc2
                        logger.error(
                            "🚨 聚合路由 %s 仍失败: %s", routing, exc2
                        )
                        _log_alert_to_journal(
                            action="swap_error",
                            message=str(exc2),
                            mint=token_mint,
                            amount_sol=approx_sol,
                            context={
                                "phase": "sell_failover",
                                "routing": routing,
                                "urgent": urgent,
                            },
                        )

            if attempt >= max_attempts:
                break
            # 逐级抬滑点重试（urgent 可突破常规 10% 硬顶，最高至 URGENT_SLIPPAGE_BPS_MAX）
            bps = risk_guard.clamp_slippage_bps(
                bps + int(C.EXIT_SELL_SLIP_STEP_BPS), urgent=urgent or bool(graduated)
            )
            # 推进路由梯队
            if urgent or graduated or C.EXIT_SELL_RETRY_NON_URGENT:
                try:
                    idx = route_ladder.index(routing)
                    if idx + 1 < len(route_ladder):
                        routing = route_ladder[idx + 1]
                except ValueError:
                    routing = "open"
            time.sleep(min(1.0 * attempt, 3.0))

    # 最后一搏：urgent / salvage 开启时强制成交
    if (urgent or C.EXIT_FORCE_SALVAGE) and last_err is not None:
        try:
            return _force_salvage(f"retries_exhausted: {last_err}")
        except Exception as exc:
            last_err = exc
    raise LiveSwapError(f"卖出在 {max_attempts} 次尝试后仍失败: {last_err}")
