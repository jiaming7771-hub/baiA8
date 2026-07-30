"""持仓金库账户 WSS 订阅：余额变动立刻推送，补强 HTTP 轮询抽池检测。

Helius / 标准节点 JSON-RPC：accountSubscribe → accountNotification。
与 HTTP RPC 一样默认直连（不走 PUMP_HTTP_PROXY）；断线自动重连。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable

from . import config as C
from . import rpc
from .onchain_price import (
    _DEAD_POOL_SOL,
    apply_vault_sol_to_position,
    sol_amount_from_account_data,
)

logger = logging.getLogger("pumpfun.vault_wss")

OnDrain = Callable[[str, dict[str, Any]], Any]


class VaultWssWatcher:
    """按持仓同步订阅 sol_vault_pubkey；推送时回写 pos 并可选触发逃生回调。"""

    def __init__(
        self,
        *,
        get_positions: Callable[[], dict[str, dict[str, Any]]],
        on_drain: OnDrain | None = None,
    ) -> None:
        self._get_positions = get_positions
        self._on_drain = on_drain
        self._stop = asyncio.Event()
        self._desired: dict[str, str] = {}  # pubkey → mint
        self._kinds: dict[str, str] = {}  # pubkey → spl|bonding
        self._sub_ids: dict[str, int] = {}  # pubkey → subscription id
        self._id_to_pubkey: dict[int, str] = {}
        self._req_id = 0
        self._pending: dict[int, str] = {}  # req id → pubkey (subscribe)
        self._ws: Any = None
        self._lock = asyncio.Lock()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        if not C.VAULT_WSS_ENABLED:
            logger.info("金库 WSS 未启用（PUMP_VAULT_WSS=0）")
            return
        backoff = 1.0
        while not self._stop.is_set():
            url = (C.SOLANA_WSS_URL or "").strip() or rpc.derive_wss_url()
            if not url:
                logger.warning("金库 WSS：无可用 URL，60s 后重试")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=60.0)
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                await self._session(url)
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("金库 WSS 会话异常，%.0fs 后重连", backoff)
            if self._stop.is_set():
                break
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(30.0, backoff * 1.8)

    async def sync_positions(self, positions: dict[str, dict[str, Any]] | None = None) -> None:
        """根据当前持仓刷新订阅集合（开仓/平仓后调用）。"""
        positions = positions if positions is not None else self._get_positions()
        desired: dict[str, str] = {}
        kinds: dict[str, str] = {}
        for mint, pos in (positions or {}).items():
            pk = (pos.get("sol_vault_pubkey") or "").strip()
            if not pk:
                continue
            desired[pk] = mint
            kinds[pk] = str(pos.get("sol_vault_kind") or "spl")
        async with self._lock:
            self._desired = desired
            self._kinds = kinds
            ws = self._ws
            if ws is None:
                return
            await self._reconcile_subs(ws)

    async def _session(self, url: str) -> None:
        import websockets

        logger.info("金库 WSS 连接 %s", rpc.redact_rpc_url(url))
        # 直连节点（与 Helius HTTP 一致）；代理环境若需再扩
        async with websockets.connect(
            url,
            ping_interval=20,
            ping_timeout=20,
            max_size=8 * 1024 * 1024,
            close_timeout=5,
        ) as ws:
            self._ws = ws
            self._sub_ids.clear()
            self._id_to_pubkey.clear()
            self._pending.clear()
            async with self._lock:
                await self._reconcile_subs(ws)
            logger.info(
                "金库 WSS 已就绪 subscriptions=%d",
                len(self._sub_ids),
            )
            try:
                while not self._stop.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=15.0)
                    except asyncio.TimeoutError:
                        # 周期性对齐订阅（持仓可能在 HTTP 路径更新了 pubkey）
                        async with self._lock:
                            await self._reconcile_subs(ws)
                        continue
                    await self._handle_message(raw)
            finally:
                self._ws = None
                self._sub_ids.clear()
                self._id_to_pubkey.clear()
                self._pending.clear()

    async def _reconcile_subs(self, ws: Any) -> None:
        want = set(self._desired)
        have = set(self._sub_ids)
        for pk in have - want:
            sub_id = self._sub_ids.pop(pk, None)
            if sub_id is not None:
                self._id_to_pubkey.pop(sub_id, None)
                self._req_id += 1
                await ws.send(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": self._req_id,
                            "method": "accountUnsubscribe",
                            "params": [sub_id],
                        }
                    )
                )
                logger.info("金库 WSS 退订 %s…", pk[:8])
        for pk in want - have:
            if pk in self._pending.values():
                continue
            self._req_id += 1
            rid = self._req_id
            self._pending[rid] = pk
            await ws.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "method": "accountSubscribe",
                        "params": [
                            pk,
                            {"encoding": "base64", "commitment": "confirmed"},
                        ],
                    }
                )
            )

    async def _handle_message(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except Exception:
            return
        if not isinstance(msg, dict):
            return

        # subscribe 应答
        if "id" in msg and "result" in msg and msg.get("id") in self._pending:
            rid = int(msg["id"])
            pk = self._pending.pop(rid, None)
            sub_id = msg.get("result")
            if pk and isinstance(sub_id, int):
                self._sub_ids[pk] = sub_id
                self._id_to_pubkey[sub_id] = pk
                logger.info("金库 WSS 订阅成功 %s… id=%s", pk[:8], sub_id)
            return

        if msg.get("method") != "accountNotification":
            return
        params = msg.get("params") or {}
        sub_id = params.get("subscription")
        if not isinstance(sub_id, int):
            return
        pk = self._id_to_pubkey.get(sub_id)
        if not pk:
            return
        mint = self._desired.get(pk)
        if not mint:
            return
        result = params.get("result") or {}
        value = result.get("value") if isinstance(result, dict) else None
        if not isinstance(value, dict):
            return
        data = value.get("data")
        data_b64 = data[0] if isinstance(data, list) and data else None
        kind = self._kinds.get(pk) or "spl"
        sol_v = sol_amount_from_account_data(data_b64, kind=kind)
        if sol_v is None:
            return
        drained = float(sol_v) <= 0.05
        positions = self._get_positions()
        pos = positions.get(mint)
        if not pos:
            return
        newly = apply_vault_sol_to_position(
            pos, sol_v, vault_drained=drained, mint=mint
        )
        if newly or pos.get("vault_drain"):
            logger.warning(
                "金库 WSS 推送 %s sol_vault=%.4f drain=%s newly=%s",
                pos.get("symbol") or mint[:6],
                sol_v,
                bool(pos.get("vault_drain")),
                newly,
            )
        if newly and self._on_drain is not None:
            maybe = self._on_drain(mint, pos)
            if asyncio.iscoroutine(maybe):
                await maybe
