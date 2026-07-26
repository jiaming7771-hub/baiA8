"""Pump.fun 超跌捡尸主循环：扫描 · 开仓 · 管仓 · STOP/回撤熔断。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
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
        self._mark_task: asyncio.Task | None = None
        self._broadcast: BroadcastFn | None = None
        self.live_wallet: str | None = None
        self.live_sol_balance: float | None = None
        self.live_bankroll: float | None = None  # 实盘启动时锚定的链上本金
        self.rpc_health: dict[str, Any] | None = None
        self._last_shadow_summary_ts: float = 0.0
        self._last_mark_log_ts: float = 0.0
        self._last_balance_recon_ts: float = 0.0
        self._mark_lock = asyncio.Lock()
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
        elif C.MICRO_LIVE:
            logger.warning(
                "🔬 小资金实盘 Micro-Live 已配置 · 单笔固定 %.3f SOL（硬顶 %.2f）· "
                "优先费=%s jito_tip=%d · 止损重试=%d次(+%dbps/次) · dry_run=%s",
                C.LIVE_SIZE_SOL,
                C.LIVE_SIZE_SOL_HARD_MAX,
                C.PRIORITY_LEVEL,
                C.JITO_TIP_LAMPORTS,
                C.EXIT_SELL_MAX_RETRIES,
                C.EXIT_SELL_SLIP_STEP_BPS,
                self.broker.dry_run,
            )
            if self.broker.dry_run:
                logger.warning(
                    "⚠️ Micro-Live 未激活真单：还需 PUMP_DRY_RUN=0 且 PUMP_LIVE_CONFIRM=1"
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
        logger.info("👻 Pump.fun 【影子交易模式 SHADOW】 strategy=%s", C.STRATEGY_MODE)
        logger.info("   行情源   : 扫描=Gecko · 持仓=链上池账户(RPC/%.0fs) DEMO_SCAN=%s",
                    C.POSITION_MARK_INTERVAL_SEC, C.DEMO_SCAN)
        logger.info("   下单     : 虚拟记账 · 禁用 Jupiter · 名义仓位 %.2f SOL", C.SHADOW_SIZE_SOL)
        logger.info(
            "   出场规则 : A硬止损-%.0f%% TP1+%.0f%%/回撤%.0f%%/时间%.0fm"
            " · B硬止损-%.0f%% TP1+%.0f%%/回撤%.0f%%/时间%.0fm · 紧急滑点≤%.0f%%",
            C.TRACK_A_HARD_STOP * 100,
            C.TRACK_A_TP1 * 100,
            C.TRACK_A_TRAIL * 100,
            C.TRACK_A_TIME_STOP,
            C.TRACK_B_HARD_STOP * 100,
            C.TRACK_B_TP1 * 100,
            C.TRACK_B_TRAIL * 100,
            C.TRACK_B_TIME_STOP,
            C.URGENT_SLIPPAGE_BPS_MAX / 100.0,
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
                    self.broker.reset_live_session(bal)
                    # 本金锚点跨重启保留（含在仓市值），否则每次重启收益率被清零
                    self.live_bankroll = float(
                        self.broker.live_bankroll_anchor or bal
                    )
                    risk_guard.peak_equity = None
                    risk_guard.drawdown_halted = False
                    risk_guard.halt_reason = None
                    risk_guard.halted_at = None
                    risk_guard.update_equity(self.broker.equity())
                    # STOP.txt 是人工/熔断拉的闸，重启不得自动清除
                    self.halted = C.STOP_FILE.exists()
                    if self.halted:
                        logger.warning("⛔ 检测到 STOP.txt，启动后维持停止开仓（持仓仍托管）")
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
        if C.MICRO_LIVE:
            logger.info(
                "   仓位模式 : 🔬 Micro-Live 单笔固定 %.3f SOL（硬顶 %.2f）· 优先费=%s max=%d lamports jito=%d",
                C.LIVE_SIZE_SOL,
                C.LIVE_SIZE_SOL_HARD_MAX,
                C.PRIORITY_LEVEL,
                C.PRIORITY_FEE_MAX_LAMPORTS,
                C.JITO_TIP_LAMPORTS,
            )
        logger.info(
            "   风控硬顶 : 入场slip≤%dbps(%.1f%%) 出场≤%dbps pos=%.1f%% [%.2f~%.2f SOL] dd≥%.0f%%或亏≥%.2fSOL",
            C.ENTRY_MAX_SLIPPAGE_BPS,
            C.ENTRY_MAX_SLIPPAGE_BPS / 100.0,
            C.MAX_SLIPPAGE_BPS,
            C.POSITION_PCT * 100,
            C.MIN_POSITION_SOL,
            C.MAX_POSITION_SOL,
            C.DRAWDOWN_HALT * 100,
            C.ABS_LOSS_HALT_SOL,
        )
        logger.info(
            "   出场规则 : A硬止损-%.0f%% TP1+%.0f%%/回撤%.0f%%/时间%.0fm"
            " · B硬止损-%.0f%% TP1+%.0f%%/回撤%.0f%%/时间%.0fm · 紧急滑点≤%.0f%%",
            C.TRACK_A_HARD_STOP * 100,
            C.TRACK_A_TP1 * 100,
            C.TRACK_A_TRAIL * 100,
            C.TRACK_A_TIME_STOP,
            C.TRACK_B_HARD_STOP * 100,
            C.TRACK_B_TP1 * 100,
            C.TRACK_B_TRAIL * 100,
            C.TRACK_B_TIME_STOP,
            C.URGENT_SLIPPAGE_BPS_MAX / 100.0,
        )
        logger.info("   监听状态 : 实盘扫描/管仓循环即将运行 demo_scan=%s", C.DEMO_SCAN)
        logger.info("=" * 60)

    @staticmethod
    def _sol_usd_for_ui() -> float:
        """看板 SOL→USD 换算；只读缓存避免在事件循环里发同步 HTTP。"""
        try:
            from .market_data import sol_usd_cached

            return float(sol_usd_cached() or 0.0)
        except Exception:
            return 0.0

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

        if shadow_on:
            stats_24h = shadow_report.stats_for_ui(
                bankroll,
                equity=round(eq, 4),
                unrealized_pnl=round(unreal, 4),
            )
        else:
            stats_24h = journal.compute_stats_24h(
                bankroll,
                equity=round(eq, 4),
                realized_pnl=round(realized, 4),
                unrealized_pnl=round(unreal, 4),
                dry_run=dry_filter,
            )

        return {
            "type": "pump_bot",
            "status": status,
            "status_label": status_label,
            "dry_run": self.broker.dry_run,
            "shadow_mode": shadow_on,
            "strategy_mode": C.STRATEGY_MODE,
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
            "sol_usd": round(float(self._sol_usd_for_ui()), 4),
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
            "stats_24h": stats_24h,
            "shadow_summary": shadow_summary,
            "trade_log": trade_log,
            "filters": {
                "strategy_mode": C.STRATEGY_MODE,
                "track_b_enabled": C.TRACK_B_ENABLED,
                # 兼容旧 UI：默认展示 A 轨
                "age_min": C.TRACK_A_AGE_MIN,
                "age_max": C.TRACK_A_AGE_MAX,
                "age_exempt_vol_m5": C.AGE_EXEMPT_VOLUME_M5_SOL,
                "age_exempt_tx_m5": C.AGE_EXEMPT_TX_M5,
                "age_exempt_bs": C.AGE_EXEMPT_BUY_SELL_RATIO,
                "rebound_min": C.TRACK_A_REBOUND_MIN,
                "rebound_max": C.TRACK_A_REBOUND_MAX,
                "rebound_strict_from": C.REBOUND_STRICT_FROM,
                "rebound_strict_bs": C.REBOUND_STRICT_BUY_SELL,
                "rebound_strict_pb": C.REBOUND_STRICT_PULLBACK,
                "buy_sell_ratio_min": C.TRACK_A_BUY_SELL_MIN,
                "pullback_max": C.TRACK_A_PULLBACK_MAX,
                "momentum_streak_min": C.MOMENTUM_STREAK_MIN,
                "ath_drop_min": C.ATH_DROP_MIN,
                "ath_drop_max": C.ATH_DROP_MAX,
                "ath_max_multiplier": C.ATH_MAX_MULTIPLIER,
                "panic_ratio_min": C.PANIC_RATIO_MIN,
                "whale_dump_min": C.WHALE_DUMP_MIN,
                "liquidity_min_sol": C.TRACK_A_LIQ_MIN,
                "min_tx_m5": C.TRACK_A_MIN_TX_M5,
                "min_volume_m5_sol": C.TRACK_A_MIN_VOL_M5,
                "hard_stop_pct": C.TRACK_A_HARD_STOP,
                "tp1_pct": C.TRACK_A_TP1,
                "tp1_sell": C.TRACK_A_TP1_SELL,
                "trail_dd": C.TRACK_A_TRAIL,
                "time_stop_m": C.TRACK_A_TIME_STOP,
                "track_a": {
                    "age_min": C.TRACK_A_AGE_MIN,
                    "age_max": C.TRACK_A_AGE_MAX,
                    "rebound_min": C.TRACK_A_REBOUND_MIN,
                    "rebound_max": C.TRACK_A_REBOUND_MAX,
                    "pullback_max": C.TRACK_A_PULLBACK_MAX,
                    "buy_sell_min": C.TRACK_A_BUY_SELL_MIN,
                    "liq_min": C.TRACK_A_LIQ_MIN,
                    "min_tx_m5": C.TRACK_A_MIN_TX_M5,
                    "min_vol_m5": C.TRACK_A_MIN_VOL_M5,
                    "hard_stop": C.TRACK_A_HARD_STOP,
                    "tp1": C.TRACK_A_TP1,
                    "tp1_sell": C.TRACK_A_TP1_SELL,
                    "trail": C.TRACK_A_TRAIL,
                    "time_stop": C.TRACK_A_TIME_STOP,
                },
                "track_b": {
                    "enabled": C.TRACK_B_ENABLED,
                    "age_min": C.TRACK_B_AGE_MIN,
                    "age_max": C.TRACK_B_AGE_MAX,
                    "pullback_max": C.TRACK_B_PULLBACK_MAX,
                    "buy_sell_min": C.TRACK_B_BUY_SELL_MIN,
                    "liq_min": C.TRACK_B_LIQ_MIN,
                    "min_tx_m5": C.TRACK_B_MIN_TX_M5,
                    "min_vol_m5": C.TRACK_B_MIN_VOL_M5,
                    "vol_spike": C.TRACK_B_VOL_SPIKE_RATIO,
                    "hard_stop": C.TRACK_B_HARD_STOP,
                    "tp1": C.TRACK_B_TP1,
                    "tp1_sell": C.TRACK_B_TP1_SELL,
                    "trail": C.TRACK_B_TRAIL,
                    "time_stop": C.TRACK_B_TIME_STOP,
                },
                "urgent_slippage_bps_max": C.URGENT_SLIPPAGE_BPS_MAX,
                "dead_cut_sec": C.DEAD_CUT_SECONDS,
                "dead_cut_pnl": C.DEAD_CUT_MIN_PNL,
                "abs_loss_halt_sol": C.ABS_LOSS_HALT_SOL,
                "shadow_size_sol": C.SHADOW_SIZE_SOL,
                "micro_live": C.MICRO_LIVE,
                "live_size_sol": C.LIVE_SIZE_SOL,
                "priority_level": C.PRIORITY_LEVEL,
                "jito_tip_lamports": C.JITO_TIP_LAMPORTS,
                "exit_sell_retries": C.EXIT_SELL_MAX_RETRIES,
                "mark_interval_sec": C.POSITION_MARK_INTERVAL_SEC,
                "price_feed": "onchain_pool",
            },
            "updated_at": self.updated_at,
            "ts": datetime.now(timezone.utc).isoformat(),
        }

    def _persist(self) -> None:
        snap = self.snapshot()
        C.STATE_FILE.write_text(
            json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def reconcile_live_balances(self) -> None:
        """实盘持仓与钱包链上 Token 余额对账：链上是唯一事实源。

        - 链上余额为 0 → 仓位视为已被外部清空（人工卖出/转移），移除并告警
        - 数量偏差 > 0.1% → 以链上余额修正本地 qty
        """
        from .chain import keypair_for_live
        from .rpc import get_token_balance_raw

        try:
            owner = str(keypair_for_live().pubkey())
        except Exception as exc:
            logger.warning("对账跳过：钱包未加载 %s", exc)
            return
        for mint, pos in list(self.broker.positions.items()):
            if pos.get("shadow") or pos.get("dry_run"):
                continue
            try:
                chain_raw, chain_dec = get_token_balance_raw(owner, mint)
            except Exception as exc:
                logger.warning("⚖️ 对账读链上余额失败 %s: %s", pos.get("symbol"), exc)
                continue
            local_raw = int(pos.get("qty_raw") or 0)
            if chain_dec:
                pos["decimals"] = chain_dec
            if chain_raw <= 0 and local_raw > 0:
                logger.error(
                    "🚨 链上余额为 0 但本地仍有持仓 %s（raw=%d）— 疑似外部卖出/转移，移除本地仓位",
                    pos.get("symbol"),
                    local_raw,
                )
                self.broker.positions.pop(mint, None)
                continue
            if local_raw > 0 and abs(chain_raw - local_raw) > max(1, local_raw // 1000):
                dec = int(pos.get("decimals") or 6)
                logger.warning(
                    "⚖️ 持仓数量对账修正 %s 本地raw=%d → 链上raw=%d",
                    pos.get("symbol"),
                    local_raw,
                    chain_raw,
                )
                pos["qty_raw"] = chain_raw
                pos["qty_left"] = chain_raw / (10 ** dec)

            self._mark_realizable(pos)

        self.broker.write_off_dust_positions()

    def _mark_realizable(self, pos: dict[str, Any]) -> None:
        """用 Jupiter 报价给持仓打「可兑现估值」。

        池子被抽干时盘口价会失真（曾出现 +586% 假涨），只有报价能兑现的 SOL 是真的。
        """
        from .live_swap import get_quote

        raw = int(pos.get("qty_raw") or 0)
        if raw <= 0:
            return
        try:
            quote = get_quote(
                input_mint=pos["mint"],
                output_mint=C.SOL_MINT,
                amount=raw,
                slippage_bps=C.MAX_SLIPPAGE_BPS,
            )
        except Exception as exc:
            logger.debug("可兑现估值报价失败 %s: %s", pos.get("symbol"), exc)
            return
        realizable = int(quote.get("outAmount") or 0) / float(C.LAMPORTS_PER_SOL)
        prev = pos.get("realizable_sol")
        pos["realizable_sol"] = realizable
        pos["realizable_ts"] = datetime.now(timezone.utc).timestamp()

        nominal = float(pos.get("qty_left") or 0) * float(pos.get("mark") or 0)
        if nominal > 0 and realizable < nominal * (1.0 - float(C.EXIT_MAX_IMPACT_PCT)):
            if prev is None or prev >= nominal * (1.0 - float(C.EXIT_MAX_IMPACT_PCT)):
                logger.error(
                    "🚨 %s 盘口估值 %.6f SOL 但只能兑现 %.6f SOL（缩水 %.1f%%）— 按可兑现值计权益",
                    pos.get("symbol"),
                    nominal,
                    realizable,
                    (1 - realizable / nominal) * 100,
                )
                journal.record_alert(
                    action="liquidity_collapse",
                    message=f"{pos.get('symbol')} 盘口价不可兑现，已按报价计权益",
                    mint=pos["mint"],
                    symbol=pos.get("symbol"),
                    context={"nominal_sol": nominal, "realizable_sol": realizable},
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

                    read_at = time.monotonic()
                    bal = await asyncio.to_thread(get_balance_sol, self.live_wallet)
                    self.live_sol_balance = bal
                    if self.live_bankroll is None:
                        self.live_bankroll = float(bal)
                        self.broker.reset_live_session(bal)
                    else:
                        self.broker.sync_live_balance(bal, read_at=read_at)
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

        # 2) 管仓：扫描价仅作 demo 兜底；实盘/影子持仓由独立 mark_loop 走链上秒级刷新
        price_map = {c["mint"]: float(c["price"]) for c in passed}

        import hashlib
        import math
        import time as _t

        now = _t.time()
        for mint, pos in list(self.broker.positions.items()):
            if mint in price_map and float(price_map[mint]) > 0:
                continue
            # 实盘/影子且非 demo：不要用假价格管仓（等待链上 mark_loop）
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

        # demo / 纸面无链上源时，扫描 tick 也顺便管仓；影子/实盘交给 mark_loop
        if C.DEMO_SCAN or (self.broker.dry_run and not self.broker.shadow and not C.SHADOW_MODE):
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
            safety_live = C.SAFETY_CHECK_ENABLED and (
                (not self.broker.dry_run and not self.broker.shadow)
                or (self.broker.shadow and C.SAFETY_ENFORCE_IN_SHADOW)
            )
            for sig in passed:
                if not sig.get("hard_pass"):
                    continue
                if len(self.broker.positions) >= C.MAX_OPEN_POSITIONS:
                    break
                if sig["mint"] in self.broker.positions:
                    continue
                # 买入前链上安全审计（防貔貅/增发/撤池）；看板标注拒绝原因
                if safety_live:
                    try:
                        from . import safety

                        verdict = await asyncio.to_thread(
                            safety.check_token_safety,
                            sig["mint"],
                            pool=sig.get("pool"),
                            dex=sig.get("dex"),
                        )
                        sig["safety_ok"] = verdict.ok
                        sig["safety_reasons"] = verdict.reasons
                        if not verdict.ok:
                            logger.warning(
                                "🚨 链上安全检查拦截 %s（未通过风控白名单）: %s",
                                sig.get("symbol") or sig["mint"][:6],
                                "; ".join(verdict.reasons),
                            )
                            continue
                    except Exception:
                        logger.exception("候选安全审计异常，跳过该币 %s", sig["mint"])
                        sig["safety_ok"] = False
                        sig["safety_reasons"] = ["安全审计异常（未通过风控白名单）"]
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
                    # 开仓后立刻拉一次链上价，避免等下一个 mark tick 才动
                    if self.broker.shadow or C.SHADOW_MODE or (not self.broker.dry_run):
                        try:
                            await self.mark_positions()
                        except Exception:
                            logger.exception("开仓后即时链上报价失败")
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

    async def mark_positions(self) -> dict[str, Any] | None:
        """秒级链上报价：候选板 + 持仓管仓 + WebSocket 推送。"""
        # demo 纸面无链上池，交给 tick 的假价格路径
        if C.DEMO_SCAN and not (self.broker.shadow or C.SHADOW_MODE or not self.broker.dry_run):
            return None
        if not self.last_scan and not self.broker.positions:
            return None

        async with self._mark_lock:
            from .onchain_price import fetch_prices_for_positions, refresh_candidate_prices

            import time as _t

            now = _t.time()
            cand_n = 0
            # ① 左侧候选板：每轮刷新展示中的链上现价（不依赖是否有持仓）
            if self.last_scan:
                try:
                    cand_n = await asyncio.to_thread(
                        refresh_candidate_prices, self.last_scan, limit=12
                    )
                except Exception:
                    logger.exception("候选链上报价失败")

            # ② 持仓管仓
            price_map: dict[str, float] = {}
            if self.broker.positions:
                try:
                    price_map = await asyncio.to_thread(
                        fetch_prices_for_positions, self.broker.positions
                    )
                except Exception:
                    logger.exception("链上持仓报价失败")

                # 持仓币若也在候选板，强制用同一链上价对齐
                for mint, px in price_map.items():
                    for row in self.last_scan:
                        if row.get("mint") == mint:
                            prev = float(row.get("price") or 0)
                            row["price"] = px
                            row["price_repr"] = f"{px:.18g}"
                            row["price_source"] = (self.broker.positions.get(mint) or {}).get(
                                "price_source"
                            )
                            row["price_ts"] = now
                            from .strategy import apply_price_drawdown

                            apply_price_drawdown(row, px)
                            if prev > 0:
                                chg = (px - prev) / prev
                                row["price_chg_pct"] = round(chg * 100.0, 4)
                                row["price_dir"] = (
                                    "up" if chg > 1e-12 else ("down" if chg < -1e-12 else "flat")
                                )
                            break

                if price_map:
                    events = await asyncio.to_thread(self.broker.manage, price_map)
                    if events:
                        self.last_events.extend(events)
                        self.last_events = self.last_events[-50:]

                # ③ 实盘：周期对账链上真实 Token 余额（每 30s，链上为唯一事实源）
                if (not self.broker.dry_run) and (not self.broker.shadow):
                    if now - self._last_balance_recon_ts >= 30:
                        self._last_balance_recon_ts = now
                        try:
                            await asyncio.to_thread(self.reconcile_live_balances)
                        except Exception:
                            logger.exception("链上持仓余额对账失败")

            # 节流日志
            if now - self._last_mark_log_ts >= 10:
                self._last_mark_log_ts = now
                parts = []
                for mint, px in list(price_map.items())[:3]:
                    pos = self.broker.positions.get(mint) or {}
                    entry = float(pos.get("entry") or 0)
                    pnl = ((px - entry) / entry * 100.0) if entry else 0.0
                    parts.append(
                        f"{pos.get('symbol', mint[:4])} {px:.10g}({pnl:+.2f}%)/{pos.get('price_source')}"
                    )
                top = self.last_scan[0] if self.last_scan else None
                logger.info(
                    "⛓ 链上报价 candidates=%d positions=%s%s",
                    cand_n,
                    " | ".join(parts) if parts else "—",
                    (
                        f" | board0={top.get('symbol')} {float(top.get('price') or 0):.10g}"
                        if top
                        else ""
                    ),
                )

            self.updated_at = datetime.now(timezone.utc).isoformat()
            self._persist()
            snap = self.snapshot()
            if self._broadcast:
                await self._broadcast(snap)
            return snap

    async def mark_loop(self) -> None:
        """秒级循环：候选板 + 持仓链上报价 → 推前端。"""
        logger.info(
            "链上报价循环启动 interval=%.1fs（候选板+持仓，RPC 直读 bonding-curve / PumpSwap）",
            C.POSITION_MARK_INTERVAL_SEC,
        )
        try:
            while self.running:
                try:
                    await self.mark_positions()
                except Exception:
                    logger.exception("mark_loop 异常")
                await asyncio.sleep(max(0.5, float(C.POSITION_MARK_INTERVAL_SEC)))
        finally:
            logger.info("链上报价循环已停止")

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
            "Pump scavenger started dry_run=%s shadow=%s strategy=%s bankroll=%.2f SOL slip_bps=%d pos_pct=%.2f%% mark=%.1fs",
            self.broker.dry_run,
            self.broker.shadow,
            C.STRATEGY_MODE,
            C.BANKROLL_SOL,
            C.MAX_SLIPPAGE_BPS,
            C.POSITION_PCT * 100,
            C.POSITION_MARK_INTERVAL_SEC,
        )
        mark_task = asyncio.create_task(self.mark_loop(), name="pumpfun-mark")
        self._mark_task = mark_task
        try:
            while self.running:
                try:
                    await self.tick()
                except Exception:
                    logger.exception("pump tick failed")
                await asyncio.sleep(C.SCAN_INTERVAL_SEC)
        finally:
            self.running = False
            mark_task.cancel()
            try:
                await mark_task
            except asyncio.CancelledError:
                pass
            self._mark_task = None
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
        if self._mark_task:
            self._mark_task.cancel()
            try:
                await self._mark_task
            except asyncio.CancelledError:
                pass
            self._mark_task = None
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
