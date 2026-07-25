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
) -> dict[str, Any]:
    bps = risk_guard.clamp_slippage_bps(slippage_bps)
    qs = urllib.parse.urlencode(
        {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": int(amount),
            "slippageBps": int(bps),
            "restrictIntermediateTokens": "true",
        }
    )
    url = f"{C.JUPITER_QUOTE_URL}?{qs}"
    quote = _http_json(url, method="GET")
    if not quote or "outAmount" not in quote:
        raise LiveSwapError(f"Jupiter 报价失败: {str(quote)[:200]}")
    return quote


def build_swap_tx(quote: dict[str, Any], user_pubkey: str) -> bytes:
    body = {
        "quoteResponse": quote,
        "userPublicKey": user_pubkey,
        "wrapAndUnwrapSol": True,
        "dynamicComputeUnitLimit": True,
        "prioritizationFeeLamports": "auto",
    }
    resp = _http_json(C.JUPITER_SWAP_URL, method="POST", payload=body)
    b64 = resp.get("swapTransaction")
    if not b64:
        raise LiveSwapError(f"Jupiter swap 无交易体: {str(resp)[:200]}")
    return base64.b64decode(b64)


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


def buy_token_with_sol(
    *,
    token_mint: str,
    sol_amount: float,
    slippage_bps: int | None = None,
    equity: float,
    cash: float,
    stop_file: bool = False,
) -> dict[str, Any]:
    """SOL → Token。开仓前强制风控。"""
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
    quote = get_quote(
        input_mint=C.SOL_MINT,
        output_mint=token_mint,
        amount=lamports,
        slippage_bps=bps,
    )
    out_amount = int(quote.get("outAmount") or 0)
    raw_tx = build_swap_tx(quote, pubkey)
    signed = sign_versioned_tx(raw_tx)
    conf = send_and_confirm(signed)
    fill_price = (sol / (out_amount / (10 ** _DEFAULT_DECIMALS))) if out_amount else 0.0
    logger.info(
        "[LIVE] BUY mint=%s… sol=%.6f out=%s sig=%s",
        token_mint[:6],
        sol,
        out_amount,
        conf["signature"][:12],
    )
    return {
        "side": "buy",
        "mint": token_mint,
        "sol_amount": sol,
        "out_amount_raw": out_amount,
        "decimals": _DEFAULT_DECIMALS,
        "qty": out_amount / (10 ** _DEFAULT_DECIMALS) if out_amount else 0.0,
        "fill_price": fill_price,
        "slippage_bps": bps,
        "signature": conf["signature"],
        "elapsed_sec": conf["elapsed_sec"],
        "quote": {
            "inAmount": quote.get("inAmount"),
            "outAmount": quote.get("outAmount"),
            "priceImpactPct": quote.get("priceImpactPct"),
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
) -> dict[str, Any]:
    """Token → SOL。平仓不受开仓熔断阻止。"""
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
    quote = get_quote(
        input_mint=token_mint,
        output_mint=C.SOL_MINT,
        amount=int(token_amount_raw),
        slippage_bps=bps,
    )
    out_lamports = int(quote.get("outAmount") or 0)
    raw_tx = build_swap_tx(quote, pubkey)
    signed = sign_versioned_tx(raw_tx)
    conf = send_and_confirm(signed)
    sol_out = out_lamports / float(C.LAMPORTS_PER_SOL)
    qty = token_amount_raw / (10 ** max(0, int(decimals)))
    fill_price = (sol_out / qty) if qty > 0 else 0.0
    logger.info(
        "[LIVE] SELL mint=%s… sol_out=%.6f sig=%s",
        token_mint[:6],
        sol_out,
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
        "slippage_bps": bps,
        "signature": conf["signature"],
        "elapsed_sec": conf["elapsed_sec"],
        "quote": {
            "inAmount": quote.get("inAmount"),
            "outAmount": quote.get("outAmount"),
            "priceImpactPct": quote.get("priceImpactPct"),
        },
    }
