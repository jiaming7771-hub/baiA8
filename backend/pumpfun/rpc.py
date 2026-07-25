"""Solana JSON-RPC 客户端：仅使用环境变量 SOLANA_RPC_URL，含超时与重试。"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
from typing import Any

from . import config as C

logger = logging.getLogger("pumpfun.rpc")

_API_KEY_RE = re.compile(r"(api-key=)([^&\s]+)", re.IGNORECASE)


class RpcError(RuntimeError):
    """RPC 调用失败（超时 / HTTP / 业务错误）。"""


def redact_rpc_url(url: str | None = None) -> str:
    """日志用：抹掉 api-key 明文。"""
    u = url or C.SOLANA_RPC_URL or ""
    return _API_KEY_RE.sub(r"\1***", u)


def get_rpc_url() -> str:
    url = (C.SOLANA_RPC_URL or "").strip()
    if not url:
        raise RpcError("SOLANA_RPC_URL 未配置")
    # 禁止在源码侧拼装密钥；只接受完整环境变量 URL
    return url


def _rpc_opener() -> urllib.request.OpenerDirector:
    """Helius 等境外 RPC 在部分网络必须走代理，否则 SSL 握手超时。"""
    proxy = (C.HTTP_PROXY or "").strip()
    if proxy:
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        return urllib.request.build_opener(handler)
    return urllib.request.build_opener()


def rpc_call(
    method: str,
    params: list[Any] | None = None,
    *,
    timeout: float | None = None,
    max_retries: int | None = None,
) -> Any:
    """同步 JSON-RPC 调用，自动重试超时与 5xx。"""
    url = get_rpc_url()
    timeout = C.RPC_TIMEOUT_SEC if timeout is None else timeout
    max_retries = C.RPC_MAX_RETRIES if max_retries is None else max_retries
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params or [],
    }
    body = json.dumps(payload).encode("utf-8")
    last_err: Exception | None = None
    opener = _rpc_opener()

    for attempt in range(1, max_retries + 1):
        try:
            req = urllib.request.Request(
                url,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with opener.open(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
            data = json.loads(raw)
            if "error" in data and data["error"]:
                raise RpcError(f"{method} error: {data['error']}")
            return data.get("result")
        except (TimeoutError, urllib.error.URLError, urllib.error.HTTPError, RpcError, OSError) as exc:
            last_err = exc
            logger.warning(
                "RPC %s 失败 attempt=%d/%d url=%s err=%s",
                method,
                attempt,
                max_retries,
                redact_rpc_url(url),
                exc,
            )
            if attempt < max_retries:
                time.sleep(min(2.0 * attempt, 6.0))
            continue
        except Exception as exc:  # pragma: no cover
            last_err = exc
            logger.exception("RPC %s 未预期异常", method)
            break

    raise RpcError(f"RPC {method} 在 {max_retries} 次重试后仍失败: {last_err}")


def get_account_info(
    pubkey: str,
    *,
    encoding: str = "base64",
    commitment: str = "confirmed",
) -> dict[str, Any] | None:
    """返回账户 value（含 owner/data/lamports），不存在则 None。"""
    result = rpc_call(
        "getAccountInfo",
        [pubkey, {"encoding": encoding, "commitment": commitment}],
        max_retries=2,
        timeout=min(12.0, C.RPC_TIMEOUT_SEC),
    )
    if not isinstance(result, dict):
        raise RpcError(f"getAccountInfo 返回异常: {result!r}")
    return result.get("value")


def get_multiple_accounts(
    pubkeys: list[str],
    *,
    encoding: str = "base64",
    commitment: str = "confirmed",
) -> list[dict[str, Any] | None]:
    """批量读账户；返回与 pubkeys 等长的 value 列表。"""
    if not pubkeys:
        return []
    result = rpc_call(
        "getMultipleAccounts",
        [pubkeys, {"encoding": encoding, "commitment": commitment}],
        max_retries=2,
        timeout=min(12.0, C.RPC_TIMEOUT_SEC),
    )
    if not isinstance(result, dict) or "value" not in result:
        raise RpcError(f"getMultipleAccounts 返回异常: {result!r}")
    values = result.get("value") or []
    out: list[dict[str, Any] | None] = []
    for i in range(len(pubkeys)):
        out.append(values[i] if i < len(values) else None)
    return out


def get_balance_lamports(pubkey: str) -> int:
    result = rpc_call("getBalance", [pubkey, {"commitment": "confirmed"}])
    if not isinstance(result, dict) or "value" not in result:
        raise RpcError(f"getBalance 返回异常: {result!r}")
    return int(result["value"])


def get_balance_sol(pubkey: str) -> float:
    return get_balance_lamports(pubkey) / float(C.LAMPORTS_PER_SOL)


def get_latest_blockhash() -> str:
    result = rpc_call("getLatestBlockhash", [{"commitment": "confirmed"}])
    if not isinstance(result, dict):
        raise RpcError("getLatestBlockhash 返回异常")
    value = result.get("value") or {}
    bh = value.get("blockhash")
    if not bh:
        raise RpcError("缺少 blockhash")
    return str(bh)


def send_raw_transaction(tx_bytes: bytes, *, skip_preflight: bool = False) -> str:
    import base64

    b64 = base64.b64encode(tx_bytes).decode("ascii")
    result = rpc_call(
        "sendTransaction",
        [
            b64,
            {
                "encoding": "base64",
                "skipPreflight": skip_preflight,
                "preflightCommitment": "confirmed",
                "maxRetries": 3,
            },
        ],
    )
    if not result:
        raise RpcError("sendTransaction 未返回签名")
    return str(result)


def confirm_signature(
    signature: str,
    *,
    timeout_sec: float | None = None,
    poll_interval: float = 1.5,
) -> dict[str, Any]:
    """等待交易确认；超时抛 RpcError，避免资金长期 Pending 卡死。"""
    timeout_sec = C.TX_CONFIRM_TIMEOUT_SEC if timeout_sec is None else timeout_sec
    deadline = time.time() + timeout_sec
    last: dict[str, Any] = {}

    while time.time() < deadline:
        try:
            statuses = rpc_call(
                "getSignatureStatuses",
                [[signature], {"searchTransactionHistory": True}],
                max_retries=2,
            )
            value = (statuses or {}).get("value") if isinstance(statuses, dict) else None
            st = (value or [None])[0] or {}
            last = st if isinstance(st, dict) else {}
            err = last.get("err")
            if err is not None:
                raise RpcError(f"交易失败 signature={signature} err={err}")
            conf = last.get("confirmationStatus")
            if conf in ("confirmed", "finalized"):
                logger.info("TX confirmed sig=%s…%s status=%s", signature[:8], signature[-6:], conf)
                return last
            if last.get("confirmations") is not None and int(last["confirmations"] or 0) >= 1:
                return last
        except RpcError:
            raise
        except Exception as exc:
            logger.warning("confirm poll 异常: %s", exc)
        time.sleep(poll_interval)

    raise RpcError(
        f"交易确认超时({timeout_sec:.0f}s) signature={signature} last={last} — 已停止等待，请人工核对链上状态"
    )


def derive_wss_url(http_url: str | None = None) -> str | None:
    """由 HTTPS RPC URL 推导 WSS（Helius / 标准节点）。"""
    u = (http_url or C.SOLANA_RPC_URL or "").strip()
    if not u:
        return None
    if u.startswith("https://"):
        return "wss://" + u[len("https://") :]
    if u.startswith("http://"):
        return "ws://" + u[len("http://") :]
    if u.startswith("wss://") or u.startswith("ws://"):
        return u
    return None


def health_check() -> dict[str, Any]:
    """启动自检：RPC 可达性（不泄露完整 URL）。"""
    t0 = time.time()
    try:
        slot = rpc_call("getSlot", [], max_retries=2, timeout=min(10.0, C.RPC_TIMEOUT_SEC))
        return {
            "ok": True,
            "slot": slot,
            "latency_ms": round((time.time() - t0) * 1000),
            "rpc": redact_rpc_url(),
            "proxy": bool((C.HTTP_PROXY or "").strip()),
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "latency_ms": round((time.time() - t0) * 1000),
            "rpc": redact_rpc_url(),
            "proxy": bool((C.HTTP_PROXY or "").strip()),
        }
