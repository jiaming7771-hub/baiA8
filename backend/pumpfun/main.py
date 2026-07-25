"""Pump.fun 超跌捡尸主循环：扫描 · 开仓 · 管仓 · STOP 熔断。"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from . import config as C
from . import journal
from .execution import PaperBroker
from .strategy import scan_market

logger = logging.getLogger("pumpfun.main")

BroadcastFn = Callable[[dict[str, Any]], Awaitable[None]]


class PumpScavengerBot:
    def __init__(self) -> None:
        self.broker = PaperBroker()
        self.running = False
        self.halted = False  # STOP.txt 熔断
        self.last_scan: list[dict[str, Any]] = []
        self.last_events: list[dict[str, Any]] = []
        self.updated_at: str | None = None
        self._task: asyncio.Task | None = None
        self._broadcast: BroadcastFn | None = None

    def stop_file_active(self) -> bool:
        return C.STOP_FILE.exists()

    def set_stop(self, active: bool) -> None:
        C.DATA_DIR.mkdir(parents=True, exist_ok=True)
        if active:
            C.STOP_FILE.write_text(
                f"STOP @ {datetime.now(timezone.utc).isoformat()}\n", encoding="utf-8"
            )
            self.halted = True
        else:
            if C.STOP_FILE.exists():
                C.STOP_FILE.unlink()
            self.halted = False

    def set_dry_run(self, dry: bool) -> None:
        self.broker.dry_run = bool(dry)

    def snapshot(self) -> dict[str, Any]:
        self.halted = self.stop_file_active()
        if self.halted:
            status = "halted"
            status_label = "已熔断停止"
        elif self.running and self.broker.dry_run:
            status = "dry_run"
            status_label = "模拟运行中"
        elif self.running:
            status = "live"
            status_label = "实盘运行中"
        else:
            status = "idle"
            status_label = "未启动"
        return {
            "type": "pump_bot",
            "status": status,
            "status_label": status_label,
            "dry_run": self.broker.dry_run,
            "halted": self.halted,
            "running": self.running,
            "bankroll_sol": C.BANKROLL_SOL,
            "equity_sol": round(self.broker.equity(), 4),
            "realized_pnl_sol": round(self.broker.net_realized(), 4),
            "gross_realized_sol": round(self.broker.gross_realized, 4),
            "total_fees_sol": round(self.broker.total_fees, 6),
            "total_slippage_sol": round(self.broker.total_slippage, 6),
            "total_gas_sol": round(self.broker.total_gas, 6),
            "unrealized_pnl_sol": round(self.broker.unrealized_pnl(), 4),
            "position_value_sol": round(self.broker.position_value(), 4),
            "cash_sol": round(self.broker.cash, 4),
            "audit_ok": bool((self.broker.last_audit or {}).get("ok", True)),
            "last_audit": self.broker.last_audit,
            "position_pct": C.POSITION_PCT,
            "max_positions": C.MAX_OPEN_POSITIONS,
            "open_count": len(self.broker.positions),
            "candidates": self.last_scan[:12],
            "positions": self.broker.snapshot_positions(),
            "events": self.last_events[-20:],
            "stats_24h": journal.compute_stats_24h(C.BANKROLL_SOL),
            "trade_log": journal.load_trades(hours=24.0, limit=80),
            "filters": {
                "age_min": C.AGE_MIN_MINUTES,
                "age_max": C.AGE_MAX_MINUTES,
                "ath_drop_min": C.ATH_DROP_MIN,
                "panic_ratio_min": C.PANIC_RATIO_MIN,
                "whale_dump_min": C.WHALE_DUMP_MIN,
                "spread_min": C.SPREAD_MIN,
                "tp1_pct": C.TP1_PCT,
                "tp1_sell": C.TP1_SELL_RATIO,
                "trail_dd": C.TRAIL_DRAWDOWN,
                "time_stop_m": C.TIME_STOP_MINUTES,
            },
            "updated_at": self.updated_at,
            "ts": datetime.now(timezone.utc).isoformat(),
        }

    def _persist(self) -> None:
        snap = self.snapshot()
        C.STATE_FILE.write_text(
            json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    async def tick(self) -> dict[str, Any]:
        self.halted = self.stop_file_active()
        # 1) 扫描
        passed = scan_market()
        self.last_scan = passed

        # 2) 管仓：扫描价优先；未扫到的持仓按 mint 独立演化（禁止共用同一收益率曲线）
        price_map = {c["mint"]: float(c["price"]) for c in passed}
        import hashlib
        import math
        import time as _t

        now = _t.time()
        for mint, pos in list(self.broker.positions.items()):
            if mint in price_map and float(price_map[mint]) > 0:
                continue
            entry = float(pos["entry"])
            if entry <= 0:
                continue
            age = max(0.0, now - float(pos["opened_at"]))
            # 每枚代币独立相位 + 振幅，避免多仓显示同一 pnl%
            seed = int(hashlib.sha256(mint.encode()).hexdigest()[:12], 16)
            phase = (seed % 6283) / 1000.0  # ~0..6.283
            amp = 0.06 + (seed % 97) / 97.0 * 0.14  # 6%~20%
            drift = ((seed % 50) - 25) / 50.0 * 0.04  # -2%~+2%/10min 量级
            wave = (
                math.sin(age / 18.0 + phase) * amp
                + math.sin(now / 11.0 + phase * 1.7) * (amp * 0.35)
                + age / 600.0 * (0.18 + drift)
            )
            price_map[mint] = max(entry * 0.35, entry * (1.0 + wave))
        events = self.broker.manage(price_map)
        if events:
            self.last_events.extend(events)
            self.last_events = self.last_events[-50:]
            # 平仓即刻推送最新权益/已实现盈亏，不等下一轮扫描
            self.updated_at = datetime.now(timezone.utc).isoformat()
            self._persist()
            if self._broadcast:
                await self._broadcast(self.snapshot())

        # 3) 开仓：熔断时禁止新开
        if not self.halted:
            for sig in passed:
                if len(self.broker.positions) >= C.MAX_OPEN_POSITIONS:
                    break
                if sig["mint"] in self.broker.positions:
                    continue
                opened = self.broker.open_long(sig)
                if opened:
                    self.last_events.append(
                        {
                            "type": "open",
                            "symbol": opened["symbol"],
                            "price": opened["entry"],
                            "dry_run": opened.get("dry_run"),
                        }
                    )

        self.updated_at = datetime.now(timezone.utc).isoformat()
        self._persist()
        snap = self.snapshot()
        if self._broadcast:
            await self._broadcast(snap)
        return snap

    async def loop(self) -> None:
        self.running = True
        logger.info(
            "Pump scavenger started dry_run=%s bankroll=%.2f SOL",
            self.broker.dry_run,
            C.BANKROLL_SOL,
        )
        try:
            while self.running:
                try:
                    await self.tick()
                except Exception:
                    logger.exception("pump tick failed")
                await asyncio.sleep(C.SCAN_INTERVAL_SEC)
        finally:
            self.running = False
            logger.info("Pump scavenger stopped")

    def start(self, broadcast: BroadcastFn | None = None) -> asyncio.Task:
        self._broadcast = broadcast
        if self._task and not self._task.done():
            return self._task
        self._task = asyncio.create_task(self.loop(), name="pumpfun-scavenger")
        return self._task

    async def stop(self) -> None:
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None


bot = PumpScavengerBot()


async def main() -> None:
    """独立 CLI 入口：python -m pumpfun.main"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    b = PumpScavengerBot()
    await b.loop()


if __name__ == "__main__":
    asyncio.run(main())
