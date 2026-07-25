"""Pump.fun 超跌捡尸主循环：扫描 · 开仓 · 管仓 · STOP/回撤熔断。"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from . import config as C
from . import journal
from . import shadow_report
from .execution import PaperBroker
from .risk import RiskBlocked, guard as risk_guard
from .strategy import scan_market

logger = logging.getLogger("pumpfun.main")

BroadcastFn = Callable[[dict[str, Any]], Awaitable[None]]


class PumpScavengerBot:
    def __init__(self) -> None:
        self.broker = PaperBroker()
        self.running = False
        self.halted = False  # STOP.txt 或回撤熔断
        self.last_scan: list[dict[str, Any]] = []
        self.last_events: list[dict[str, Any]] = []
        self.updated_at: str | None = None
        self._task: asyncio.Task | None = None
        self._broadcast: BroadcastFn | None = None
        self.live_wallet: str | None = None
        self.live_sol_balance: float | None = None
        self.live_bankroll: float | None = None  # 实盘启动时锚定的链上本金
        self.rpc_health: dict[str, Any] | None = None
        self._last_shadow_summary_ts: float = 0.0
        # 初始化峰值权益
        risk_guard.update_equity(self.broker.equity())
        if C.SHADOW_MODE:
            self.broker.shadow = True
            # 影子模式默认不发真单：即使 .env 写了 LIVE 也锁住
            self.broker.dry_run = True
            logger.warning(
                "👻 影子交易模式已启用 SHADOW_MODE=1 · 真行情 · 虚拟成交 · 单笔 %.2f SOL",
                C.SHADOW_SIZE_SOL,
            )

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
            risk_guard.reset_halt()

    def set_dry_run(self, dry: bool) -> None:
        """切换模拟/实盘。切到 LIVE 时强制校验钱包 + LIVE_CONFIRM。"""
        if C.SHADOW_MODE or self.broker.shadow:
            # 影子模式禁止切真单
            self.broker.dry_run = True
            self.broker.shadow = True
            logger.warning("影子模式中，忽略 dry_run 切换（保持虚拟成交）")
            return
        want_live = not bool(dry)
        if want_live:
            if not C.LIVE_CONFIRM:
                raise RuntimeError(
                    "拒绝 LIVE：请在 .env 设置 PUMP_LIVE_CONFIRM=1（与 PUMP_DRY_RUN=0 一起）"
                )
            from .chain import get_signer
            from wallet import WalletConfigError

            try:
                get_signer().ensure_loaded()
            except WalletConfigError as exc:
                logger.error("拒绝切换 LIVE：%s", exc)
                raise
            self._log_live_banner()
            logger.warning(
                "Pump 已切到 LIVE · 签名钱包 %s",
                get_signer().pubkey[:4] + "…" + get_signer().pubkey[-4:],
            )
        self.broker.dry_run = bool(dry)

    def _log_shadow_banner(self) -> None:
        logger.info("=" * 60)
        logger.info("👻 Pump.fun 【影子交易模式 SHADOW】")
        logger.info("   行情源   : 真实（Gecko/DexScreener，DEMO_SCAN=%s）", C.DEMO_SCAN)
        logger.info("   下单     : 虚拟记账 · 禁用 Jupiter · 名义仓位 %.2f SOL", C.SHADOW_SIZE_SOL)
        logger.info(
            "   出场规则 : 硬止损-%.0f%% | TP1+%.0f%%卖%.0f%% | 移动回撤%.0f%% | 时间%.0fm",
            C.HARD_STOP_PCT * 100,
            C.TP1_PCT * 100,
            C.TP1_SELL_RATIO * 100,
            C.TRAIL_DRAWDOWN * 100,
            C.TIME_STOP_MINUTES,
        )
        logger.info(
            "   报告文件 : %s | %s",
            C.SHADOW_TRADES_FILE.name,
            C.SHADOW_SUMMARY_FILE.name,
        )
        logger.info("=" * 60)
        shadow_report.print_summary()

    def _log_live_banner(self) -> None:
        """启动/切 LIVE 时打印钱包地址、SOL 余额、RPC 监听状态。"""
        from .rpc import get_balance_sol, health_check, redact_rpc_url
        from wallet import get_pubkey_str, wallet_status

        st = wallet_status()
        pubkey = st.get("pubkey") or get_pubkey_str()
        self.live_wallet = pubkey
        self.rpc_health = health_check()
        bal = None
        if pubkey and self.rpc_health.get("ok"):
            try:
                bal = get_balance_sol(pubkey)
                self.live_sol_balance = bal
                # 实盘：链上真实余额是唯一可用现金基准；丢弃纸面历史账户，
                # 并以真实权益重新锚定风控峰值，避免纸面 equity 造成误判熔断。
                if bal is not None and bal > 0:
                    # 实盘会话：以链上余额为唯一本金/权益基准，切断纸面历史污染
                    self.live_bankroll = float(bal)
                    self.broker.reset_live_session(bal)
                    risk_guard.peak_equity = None
                    risk_guard.drawdown_halted = False
                    risk_guard.halt_reason = None
                    risk_guard.halted_at = None
                    risk_guard.update_equity(self.broker.equity())
                    # 清除因纸面基准误写的 STOP.txt
                    if C.STOP_FILE.exists():
                        try:
                            C.STOP_FILE.unlink()
                        except Exception:
                            logger.warning("清除误报 STOP.txt 失败")
                    self.halted = False
                    logger.info(
                        "实盘基准已锚定：bankroll=cash=%.6f SOL peak=%.6f SOL（纸面历史已隔离）",
                        self.broker.cash,
                        risk_guard.peak_equity or self.broker.equity(),
                    )
            except Exception as exc:
                logger.error("读取 SOL 余额失败: %s", exc)

        mode = "LIVE" if not self.broker.dry_run else "切换中→LIVE"
        logger.info("=" * 60)
        logger.info("🔴 Pump.fun 【实盘模式 %s】", mode)
        logger.info("   钱包地址 : %s", pubkey or "(未加载)")
        logger.info(
            "   SOL 余额 : %s",
            f"{bal:.6f} SOL" if bal is not None else "(读取失败)",
        )
        logger.info(
            "   RPC 状态 : %s | %s | slot=%s latency=%sms",
            "OK" if (self.rpc_health or {}).get("ok") else "FAIL",
            redact_rpc_url(),
            (self.rpc_health or {}).get("slot"),
            (self.rpc_health or {}).get("latency_ms"),
        )
        logger.info(
            "   风控硬顶 : slip≤%dbps(%.1f%%) pos=%.1f%% [%.2f~%.2f SOL] dd≥%.0f%%或亏≥%.2fSOL",
            C.MAX_SLIPPAGE_BPS,
            C.MAX_SLIPPAGE_BPS / 100.0,
            C.POSITION_PCT * 100,
            C.MIN_POSITION_SOL,
            C.MAX_POSITION_SOL,
            C.DRAWDOWN_HALT * 100,
            C.ABS_LOSS_HALT_SOL,
        )
        logger.info(
            "   出场规则 : 硬止损-%.0f%% | TP1+%.0f%%卖%.0f%% | 移动回撤%.0f%% | 时间%.0fm",
            C.HARD_STOP_PCT * 100,
            C.TP1_PCT * 100,
            C.TP1_SELL_RATIO * 100,
            C.TRAIL_DRAWDOWN * 100,
            C.TIME_STOP_MINUTES,
        )
        logger.info("   监听状态 : 实盘扫描/管仓循环即将运行 demo_scan=%s", C.DEMO_SCAN)
        logger.info("=" * 60)

    def snapshot(self) -> dict[str, Any]:
        self.halted = self.stop_file_active() or risk_guard.drawdown_halted
        if self.halted:
            status = "halted"
            status_label = "已熔断停止"
        elif self.running and (self.broker.shadow or C.SHADOW_MODE):
            status = "shadow"
            status_label = "影子交易运行中"
        elif self.running and self.broker.dry_run:
            status = "dry_run"
            status_label = "模拟运行中"
        elif self.running:
            status = "live"
            status_label = "实盘运行中"
        else:
            status = "idle"
            status_label = "未启动"

        wallet_info: dict[str, Any] = {"configured": False}
        try:
            from wallet import wallet_status

            wallet_info = wallet_status()
        except Exception as exc:  # pragma: no cover
            wallet_info = {"configured": False, "error": str(exc)}

        eq = self.broker.equity()
        risk_info = risk_guard.update_equity(eq)
        realized = self.broker.net_realized()
        unreal = self.broker.unrealized_pnl()
        shadow_on = bool(self.broker.shadow or C.SHADOW_MODE)
        # 实盘：本金用链上锚定值；纸面/影子：用配置本金。杜绝 10SOL 纸面本金 vs 3SOL 链上权益混算。
        if (not self.broker.dry_run) and (not shadow_on):
            bankroll = float(
                self.live_bankroll
                if self.live_bankroll is not None
                else (self.live_sol_balance if self.live_sol_balance is not None else self.broker.bankroll)
            )
            dry_filter: bool | None = False
        else:
            bankroll = float(self.broker.bankroll or C.BANKROLL_SOL)
            dry_filter = True

        trade_log = journal.load_trades(hours=24.0, limit=80)
        if shadow_on:
            trade_log = [t for t in trade_log if bool(t.get("shadow"))]
        elif dry_filter is True:
            trade_log = [t for t in trade_log if bool(t.get("dry_run", True)) and not t.get("shadow")]
        elif dry_filter is False:
            trade_log = [t for t in trade_log if not bool(t.get("dry_run", True)) and not t.get("shadow")]

        mode = "shadow" if shadow_on else ("dry_run" if self.broker.dry_run else "live")
        shadow_summary = shadow_report.get_summary() if shadow_on else None

        return {
            "type": "pump_bot",
            "status": status,
            "status_label": status_label,
            "dry_run": self.broker.dry_run,
            "shadow_mode": shadow_on,
            "mode": mode,
            "halted": self.halted,
            "running": self.running,
            "wallet": wallet_info,
            "live_wallet": self.live_wallet,
            "live_sol_balance": self.live_sol_balance,
            "live_bankroll": self.live_bankroll,
            "rpc_health": self.rpc_health,
            "risk": risk_guard.snapshot(),
            "risk_check": risk_info,
            "bankroll_sol": round(bankroll, 4),
            "equity_sol": round(eq, 4),
            "realized_pnl_sol": round(realized, 4),
            "gross_realized_sol": round(self.broker.gross_realized, 4),
            "total_fees_sol": round(self.broker.total_fees, 6),
            "total_slippage_sol": round(self.broker.total_slippage, 6),
            "total_gas_sol": round(self.broker.total_gas, 6),
            "unrealized_pnl_sol": round(unreal, 4),
            "position_value_sol": round(self.broker.position_value(), 4),
            "cash_sol": round(self.broker.cash, 4),
            "audit_ok": bool((self.broker.last_audit or {}).get("ok", True)),
            "last_audit": self.broker.last_audit,
            "position_pct": C.POSITION_PCT,
            "max_slippage_bps": C.MAX_SLIPPAGE_BPS,
            "max_pos_sol": C.MAX_POSITION_SOL,
            "min_pos_sol": C.MIN_POSITION_SOL,
            "drawdown_halt": C.DRAWDOWN_HALT,
            "max_positions": C.MAX_OPEN_POSITIONS,
            "open_count": len(self.broker.positions),
            "candidates": self.last_scan[:12],
            "positions": self.broker.snapshot_positions(),
            "events": self.last_events[-20:],
            "stats_24h": journal.compute_stats_24h(
                bankroll,
                equity=round(eq, 4),
                realized_pnl=round(realized, 4),
                unrealized_pnl=round(unreal, 4),
                dry_run=dry_filter,
            ),
            "shadow_summary": shadow_summary,
            "trade_log": trade_log,
            "filters": {
                "age_min": C.AGE_MIN_MINUTES,
                "age_max": C.AGE_MAX_MINUTES,
                "ath_drop_min": C.ATH_DROP_MIN,
                "ath_drop_max": C.ATH_DROP_MAX,
                "ath_max_multiplier": C.ATH_MAX_MULTIPLIER,
                "panic_ratio_min": C.PANIC_RATIO_MIN,
                "whale_dump_min": C.WHALE_DUMP_MIN,
                "liquidity_min_sol": C.LIQUIDITY_MIN_SOL,
                "min_tx_m5": C.MIN_TX_M5,
                "min_volume_m5_sol": C.MIN_VOLUME_M5_SOL,
                "hard_stop_pct": C.HARD_STOP_PCT,
                "tp1_pct": C.TP1_PCT,
                "tp1_sell": C.TP1_SELL_RATIO,
                "trail_dd": C.TRAIL_DRAWDOWN,
                "time_stop_m": C.TIME_STOP_MINUTES,
                "abs_loss_halt_sol": C.ABS_LOSS_HALT_SOL,
                "shadow_size_sol": C.SHADOW_SIZE_SOL,
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
        stop_file = self.stop_file_active()

        # 实盘：每轮用链上余额校准空仓权益（阻塞 RPC 放线程池，且限频 30s）
        # 影子模式不碰链上余额
        if (not self.broker.dry_run) and (not self.broker.shadow) and self.live_wallet:
            now_ts = datetime.now(timezone.utc).timestamp()
            if now_ts - getattr(self, "_last_bal_sync", 0.0) >= 30.0:
                self._last_bal_sync = now_ts
                try:
                    from .rpc import get_balance_sol

                    bal = await asyncio.to_thread(get_balance_sol, self.live_wallet)
                    self.live_sol_balance = bal
                    if self.live_bankroll is None:
                        self.live_bankroll = float(bal)
                        self.broker.reset_live_session(bal)
                    else:
                        self.broker.sync_live_balance(bal)
                except Exception as exc:
                    logger.warning("实盘余额同步失败: %s", exc)

        # 回撤熔断检查（更新 peak / 可能触发 halt）
        risk_info = risk_guard.update_equity(self.broker.equity())
        self.halted = stop_file or risk_guard.drawdown_halted
        if risk_guard.drawdown_halted and not stop_file:
            # 自动落 STOP 文件，持久化熔断
            try:
                if not C.STOP_FILE.exists():
                    self.set_stop(True)
                    logger.error("🚨 回撤熔断已写入 STOP.txt")
            except Exception:
                logger.exception("写入 STOP.txt 失败")

        # 1) 扫描（真实行情为阻塞 HTTP，放线程池，避免卡死事件循环/API）
        passed = await asyncio.to_thread(scan_market)
        self.last_scan = passed

        # 2) 管仓：扫描价优先；未扫到的持仓按 mint 独立演化（禁止共用同一收益率曲线）
        price_map = {c["mint"]: float(c["price"]) for c in passed}

        # 实盘 / 影子：持仓每轮优先走 DexScreener 独立刷新；
        # 扫描观察池价仅作兜底，避免 45s/90s 扫描节流拖慢 11 分钟管仓。
        need_real_px = bool(self.broker.positions) and (
            (not self.broker.dry_run) or self.broker.shadow or C.SHADOW_MODE
        )
        if need_real_px:
            def _refresh_position_prices() -> None:
                from .market_data import fetch_token_price_sol, latest_price_map

                live_px = latest_price_map()
                for mint in list(self.broker.positions):
                    px = fetch_token_price_sol(mint)
                    if not px:
                        px = price_map.get(mint) or live_px.get(mint)
                    if px and px > 0:
                        price_map[mint] = float(px)
                    else:
                        logger.warning("持仓 %s 无实时价，本轮沿用上次 mark", mint[:8])

            try:
                await asyncio.to_thread(_refresh_position_prices)
            except Exception:
                logger.exception("持仓价格刷新失败")

        import hashlib
        import math
        import time as _t

        now = _t.time()
        for mint, pos in list(self.broker.positions.items()):
            if mint in price_map and float(price_map[mint]) > 0:
                continue
            # 实盘/影子且非 demo：不要用假价格管仓
            if ((not self.broker.dry_run) or self.broker.shadow) and (not C.DEMO_SCAN):
                continue
            entry = float(pos["entry"])
            if entry <= 0:
                continue
            age = max(0.0, now - float(pos["opened_at"]))
            seed = int(hashlib.sha256(mint.encode()).hexdigest()[:12], 16)
            phase = (seed % 6283) / 1000.0
            amp = 0.06 + (seed % 97) / 97.0 * 0.14
            drift = ((seed % 50) - 25) / 50.0 * 0.04
            wave = (
                math.sin(age / 18.0 + phase) * amp
                + math.sin(now / 11.0 + phase * 1.7) * (amp * 0.35)
                + age / 600.0 * (0.18 + drift)
            )
            price_map[mint] = max(entry * 0.35, entry * (1.0 + wave))
        # 管仓可能触发链上卖出（阻塞 HTTP），放线程池
        events = await asyncio.to_thread(self.broker.manage, price_map)
        if events:
            self.last_events.extend(events)
            self.last_events = self.last_events[-50:]
            self.updated_at = datetime.now(timezone.utc).isoformat()
            self._persist()
            if self._broadcast:
                await self._broadcast(self.snapshot())

        # 影子：每 5 分钟打印一次汇总
        if self.broker.shadow or C.SHADOW_MODE:
            if now - self._last_shadow_summary_ts >= 300:
                self._last_shadow_summary_ts = now
                try:
                    await asyncio.to_thread(shadow_report.print_summary)
                except Exception:
                    logger.exception("影子汇总打印失败")

        # 3) 开仓：STOP / 回撤熔断时禁止新开；只吃 hard_pass
        if not self.halted:
            for sig in passed:
                if not sig.get("hard_pass"):
                    continue
                if len(self.broker.positions) >= C.MAX_OPEN_POSITIONS:
                    break
                if sig["mint"] in self.broker.positions:
                    continue
                try:
                    # 实盘开仓走 Jupiter（阻塞 HTTP），放线程池
                    opened = await asyncio.to_thread(
                        self.broker.open_long, sig, stop_file=stop_file
                    )
                except RiskBlocked as exc:
                    logger.error("开仓风控拦截: %s", exc)
                    opened = None
                if opened:
                    self.last_events.append(
                        {
                            "type": "open",
                            "symbol": opened["symbol"],
                            "price": opened["entry"],
                            "dry_run": opened.get("dry_run"),
                            "shadow": opened.get("shadow"),
                            "tx_signature": opened.get("tx_signature"),
                        }
                    )
                    break  # 每轮最多开 1 笔
        else:
            logger.warning(
                "熔断中，跳过新开仓 stop_file=%s dd_halt=%s dd=%s",
                stop_file,
                risk_guard.drawdown_halted,
                risk_info.get("drawdown"),
            )

        self.updated_at = datetime.now(timezone.utc).isoformat()
        self._persist()
        snap = self.snapshot()
        if self._broadcast:
            await self._broadcast(snap)
        return snap

    async def loop(self) -> None:
        self.running = True
        if self.broker.shadow or C.SHADOW_MODE:
            try:
                self._log_shadow_banner()
            except Exception:
                logger.exception("影子启动横幅失败")
        elif not self.broker.dry_run:
            try:
                self._log_live_banner()
            except Exception:
                logger.exception("实盘启动横幅失败")
        else:
            logger.info(
                "Pump scavenger started [DRY_RUN] bankroll=%.2f SOL",
                C.BANKROLL_SOL,
            )
        logger.info(
            "Pump scavenger started dry_run=%s shadow=%s bankroll=%.2f SOL slip_bps=%d pos_pct=%.2f%%",
            self.broker.dry_run,
            self.broker.shadow,
            C.BANKROLL_SOL,
            C.MAX_SLIPPAGE_BPS,
            C.POSITION_PCT * 100,
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
            if self.broker.shadow or C.SHADOW_MODE:
                try:
                    shadow_report.print_summary()
                except Exception:
                    pass
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
