"""模拟下单与止损/止盈生命周期。"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from . import config as C
from . import journal
from . import shadow_report
from .risk import RiskBlocked, guard as risk_guard

import audit_ledger as AL
from audit_ledger import pump_ledger

logger = logging.getLogger("pumpfun.execution")


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pos_metrics(pos: dict[str, Any], signal: dict[str, Any] | None = None) -> dict[str, Any]:
    src = signal or {}
    return {
        "panic_ratio": src.get("panic_ratio", pos.get("panic_ratio")),
        "ath_drop_pct": src.get("ath_drop_pct", pos.get("ath_drop_pct")),
        "whale_dump_pct": src.get("whale_dump_pct", pos.get("whale_dump_pct")),
        "tx_count_m5": src.get("tx_count_m5", pos.get("tx_count_m5")),
        "volume_m5_sol": src.get("volume_m5_sol", pos.get("volume_m5_sol")),
        "volume_m5_usd": src.get("volume_m5_usd", pos.get("volume_m5_usd")),
        "score": src.get("score", pos.get("score")),
        "age_minutes": src.get("age_minutes", pos.get("signal_age_minutes")),
        "slippage_pct": src.get("slippage_pct") or pos.get("slippage_pct") or 0.15,
        "fee_sol": pos.get("fees_sol"),
        "gas_sol": pos.get("gas_sol"),
        "slippage_sol": pos.get("slippage_sol"),
        "shadow": bool(pos.get("shadow")),
        "max_float_pnl_pct": pos.get("max_float_pnl_pct"),
    }


class PaperBroker:
    """极度保守的纸面执行器（默认 DRY_RUN），含 DEX 手续费/Gas/滑点清算。"""

    def __init__(self) -> None:
        self.bankroll = C.BANKROLL_SOL
        self.cash = C.BANKROLL_SOL
        self.positions: dict[str, dict[str, Any]] = {}
        self.gross_realized = 0.0
        self.total_fees = 0.0
        self.total_slippage = 0.0
        self.total_gas = 0.0
        self.realized_pnl = 0.0  # 净已实现
        self.dry_run = C.DRY_RUN_DEFAULT
        self.shadow = bool(C.SHADOW_MODE)
        self.last_audit: dict[str, Any] | None = None
        self._restore_account()
        if self.shadow:
            # 影子模式：虚拟本金，避免污染实盘账户口径
            self.bankroll = max(self.bankroll, C.SHADOW_SIZE_SOL * 10)
            self.cash = self.bankroll
            self.gross_realized = 0.0
            self.total_fees = 0.0
            self.total_slippage = 0.0
            self.total_gas = 0.0
            self.realized_pnl = 0.0
            logger.warning(
                "👻 SHADOW_MODE=ON · 真行情喂价 · 虚拟成交（禁用 Jupiter）· 单笔名义 %.2f SOL",
                C.SHADOW_SIZE_SOL,
            )

    def net_realized(self) -> float:
        return self.gross_realized - self.total_fees - self.total_slippage - self.total_gas

    # ---------- 账户持久化 ----------
    def _restore_account(self) -> None:
        """优先读账户文件；缺失时用历史成交/账本重建。"""
        try:
            if C.ACCOUNT_FILE.exists():
                saved = json.loads(C.ACCOUNT_FILE.read_text(encoding="utf-8"))
                self.gross_realized = float(saved.get("gross_realized_sol") or 0.0)
                self.total_fees = float(saved.get("total_fees_sol") or 0.0)
                self.total_slippage = float(saved.get("total_slippage_sol") or 0.0)
                self.total_gas = float(saved.get("total_gas_sol") or 0.0)
                self.realized_pnl = float(
                    saved.get("realized_pnl_sol")
                    if saved.get("realized_pnl_sol") is not None
                    else self.net_realized()
                )
                self.cash = float(saved.get("cash_sol") or (self.bankroll + self.realized_pnl))
                if not self.positions:
                    self.cash = self.bankroll + self.net_realized()
                    self.realized_pnl = self.net_realized()
                logger.info(
                    "账户已恢复 cash=%.6f net=%.6f fees=%.6f slip=%.6f gas=%.6f",
                    self.cash, self.realized_pnl, self.total_fees, self.total_slippage, self.total_gas,
                )
                return
            sums = pump_ledger.sum_costs()
            if sums["entry_count"]:
                self.gross_realized = sums["gross_realized"]
                self.total_fees = sums["fees"]
                self.total_slippage = sums["slippage"]
                self.total_gas = sums["gas"]
                self.realized_pnl = sums["net_realized"]
                self.cash = self.bankroll + self.realized_pnl
                logger.info("账户由复式账本重建 net=%.6f", self.realized_pnl)
                return
            self.realized_pnl = journal.lifetime_realized_pnl()
            self.gross_realized = self.realized_pnl
            self.cash = self.bankroll + self.realized_pnl
            if self.realized_pnl:
                logger.info("账户由历史成交重建 realized=%.6f", self.realized_pnl)
        except Exception:
            logger.exception("账户恢复失败，回落到初始本金")
            self.gross_realized = 0.0
            self.total_fees = 0.0
            self.total_slippage = 0.0
            self.total_gas = 0.0
            self.realized_pnl = 0.0
            self.cash = self.bankroll
        self._persist_account()

    def _persist_account(self) -> None:
        try:
            C.DATA_DIR.mkdir(parents=True, exist_ok=True)
            self.realized_pnl = self.net_realized()
            payload = {
                "bankroll_sol": round(self.bankroll, 8),
                "cash_sol": round(self.cash, 8),
                "gross_realized_sol": round(self.gross_realized, 8),
                "total_fees_sol": round(self.total_fees, 8),
                "total_slippage_sol": round(self.total_slippage, 8),
                "total_gas_sol": round(self.total_gas, 8),
                "realized_pnl_sol": round(self.realized_pnl, 8),
                "open_positions": len(self.positions),
                "updated_at": _utc(),
            }
            tmp = C.ACCOUNT_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(C.ACCOUNT_FILE)
        except Exception:
            logger.exception("账户持久化失败")

    def _charge_friction(
        self,
        *,
        amount_sol: float,
        side: str,
        pos: dict[str, Any],
        note: str,
        slip_pct_override: float | None = None,
        write_ledger: bool = True,
    ) -> dict[str, float]:
        costs = AL.pump_trade_costs(amount_sol=amount_sol, side=side)
        # 影子模式：用可配置滑点覆盖乐观默认值，贴近实盘 pump 币真实滑移
        if slip_pct_override is not None:
            slip_amt = float(amount_sol) * float(slip_pct_override)
            costs["slippage_sol"] = round(slip_amt, 8)
            costs["slippage_pct"] = float(slip_pct_override)
        fee, gas, slip = costs["fee_sol"], costs["gas_sol"], costs["slippage_sol"]
        self.cash -= fee + gas + slip
        self.total_fees += fee
        self.total_gas += gas
        self.total_slippage += slip
        pos["fees_sol"] = float(pos.get("fees_sol") or 0) + fee
        pos["gas_sol"] = float(pos.get("gas_sol") or 0) + gas
        pos["slippage_sol"] = float(pos.get("slippage_sol") or 0) + slip
        # 影子账户为虚拟，不写实盘审计账本，避免污染 pump_ledger
        if write_ledger:
            pump_ledger.append({"kind": "dex_fee", "amount": fee, "symbol": pos.get("symbol"), "position_id": pos.get("id"), "note": note, "meta": costs})
            pump_ledger.append({"kind": "gas", "amount": gas, "symbol": pos.get("symbol"), "position_id": pos.get("id"), "note": note, "meta": costs})
            pump_ledger.append({"kind": "slippage", "amount": slip, "symbol": pos.get("symbol"), "position_id": pos.get("id"), "note": note, "meta": costs})
        return costs

    def position_size_sol(self) -> float:
        """单笔仓位：权益×1%~2%，硬夹 0.02~0.04 SOL。"""
        try:
            return risk_guard.clamp_position_sol(
                self.equity() * C.POSITION_PCT,
                equity=self.equity(),
                cash=self.cash,
            )
        except RiskBlocked:
            return 0.0

    def open_long(
        self,
        signal: dict[str, Any],
        *,
        dry_run: bool | None = None,
        stop_file: bool = False,
    ) -> dict[str, Any] | None:
        dry = self.dry_run if dry_run is None else dry_run
        shadow = bool(self.shadow or C.SHADOW_MODE)
        # 影子模式强制虚拟成交：绝不走 Jupiter
        if shadow:
            dry = True
        mint = signal["mint"]
        if mint in self.positions:
            return None
        if len(self.positions) >= C.MAX_OPEN_POSITIONS:
            return None

        # 绑定池地址（开仓后管仓直接读链上账户，不再走 DexScreener）
        if not signal.get("pool"):
            try:
                from .market_data import lookup_pool

                pool, dex = lookup_pool(mint)
                if pool:
                    signal = {**signal, "pool": pool, "dex": dex or signal.get("dex")}
            except Exception:
                pass

        # 入场价优先用链上池价，避免 Gecko 扫描价滞后造成「刚买就亏 xx%」的假象
        mid = float(signal["price"])
        onchain_meta = None
        try:
            from .onchain_price import fetch_pool_price_sol

            onchain_meta = fetch_pool_price_sol(
                mint, pool=signal.get("pool"), dex=signal.get("dex")
            )
            if onchain_meta and float(onchain_meta.get("price") or 0) > 0:
                chain_px = float(onchain_meta["price"])
                if mid > 0:
                    drift = (chain_px - mid) / mid
                    if abs(drift) >= 0.02:
                        logger.warning(
                            "开仓改用链上价 %s gecko=%.8g chain=%.8g drift=%+.1f%% src=%s",
                            signal.get("symbol") or mint[:6],
                            mid,
                            chain_px,
                            drift * 100,
                            onchain_meta.get("source"),
                        )
                mid = chain_px
                if onchain_meta.get("pool") and not signal.get("pool"):
                    signal = {**signal, "pool": onchain_meta["pool"]}
        except Exception:
            logger.exception("开仓链上询价失败，回退信号价 %s", mint[:8])
        if mid <= 0:
            return None

        live_meta: dict[str, Any] = {}
        if shadow:
            # 影子：固定名义仓位（默认 1 SOL），跳过实盘仓位硬顶夹紧
            sol = max(0.001, float(C.SHADOW_SIZE_SOL))
            if sol > self.cash:
                logger.warning("影子开仓跳过：虚拟现金不足 cash=%.4f need=%.4f", self.cash, sol)
                return None
            if stop_file:
                logger.warning("影子开仓跳过：STOP.txt 生效")
                return None
            slip_bps = int(C.MAX_SLIPPAGE_BPS)
            qty = sol / mid
        else:
            # ---- 硬风控：开仓前必须通过（滑点/仓位/回撤熔断）----
            try:
                gate = risk_guard.pre_trade_gate(
                    side="buy",
                    equity=self.equity(),
                    cash=self.cash,
                    amount_sol=self.equity() * C.POSITION_PCT,
                    slippage_bps=C.MAX_SLIPPAGE_BPS,
                    stop_file=stop_file,
                )
            except RiskBlocked as exc:
                logger.error("开仓被风控拦截: %s", exc)
                return None

            sol = float(gate["amount_sol"])
            slip_bps = int(gate["slippage_bps"])

            if not dry:
                # LIVE：钱包 + Jupiter 实盘换币
                from .live_swap import LiveSwapError, buy_token_with_sol

                try:
                    from .chain import keypair_for_live

                    _kp = keypair_for_live()
                    logger.info(
                        "LIVE open 钱包 %s…%s rpc 滑点硬顶=%dbps(%.1f%%)",
                        str(_kp.pubkey())[:4],
                        str(_kp.pubkey())[-4:],
                        slip_bps,
                        slip_bps / 100.0,
                    )
                    live_meta = buy_token_with_sol(
                        token_mint=mint,
                        sol_amount=sol,
                        slippage_bps=slip_bps,
                        equity=self.equity(),
                        cash=self.cash,
                        stop_file=stop_file,
                    )
                    sol = float(live_meta.get("sol_amount") or sol)
                    if live_meta.get("qty"):
                        qty = float(live_meta["qty"])
                    else:
                        qty = sol / mid
                    if live_meta.get("fill_price"):
                        mid = float(live_meta["fill_price"])
                except (RiskBlocked, LiveSwapError, Exception) as exc:
                    logger.error("LIVE 开仓中止：%s", exc)
                    return None
            else:
                preview = AL.pump_trade_costs(amount_sol=sol, side="buy")
                if sol + preview["total_friction_sol"] > self.cash:
                    return None
                qty = sol / mid

        costs = AL.pump_trade_costs(amount_sol=sol, side="buy")
        # 实盘：用硬顶滑点覆盖纸面默认
        costs["slippage_pct"] = slip_bps / 10_000.0
        shadow_slip_pct = float(C.SHADOW_SLIPPAGE_BPS) / 10_000.0
        if shadow:
            # 影子按含滑点成交价记账（贴近实盘），成交价影响 fill 记录
            fill_px = AL.pump_fill_price(mid, side="buy", slip_pct=shadow_slip_pct)
        else:
            fill_px = float(live_meta.get("fill_price") or AL.pump_fill_price(
                mid, side="buy", slip_pct=costs["slippage_pct"]
            ))
        pos = {
            "id": str(uuid.uuid4())[:8],
            "mint": mint,
            "symbol": signal.get("symbol") or mint[:6],
            "entry": mid,
            "qty": qty,
            "qty_left": qty,
            "qty_raw": int(live_meta.get("out_amount_raw") or 0),
            "decimals": int(live_meta.get("decimals") or 6),
            "sol_spent": sol,
            "opened_at": time.time(),
            "opened_at_iso": _utc(),
            "peak": mid,
            "tp1_done": False,
            "trail_line": None,
            "dry_run": dry,
            "shadow": shadow,
            "status": "open",
            "pool": signal.get("pool"),
            "dex": signal.get("dex"),
            "price_source": (onchain_meta or {}).get("source") or "signal",
            "price_ts": time.time(),
            "mark": mid,
            "score": signal.get("score"),
            "ath_drop_pct": signal.get("ath_drop_pct"),
            "panic_ratio": signal.get("panic_ratio"),
            "whale_dump_pct": signal.get("whale_dump_pct"),
            "tx_count_m5": signal.get("tx_count_m5"),
            "volume_m5_sol": signal.get("volume_m5_sol"),
            "volume_m5_usd": signal.get("volume_m5_usd"),
            "signal_age_minutes": signal.get("age_minutes"),
            "slippage_pct": shadow_slip_pct if shadow else costs["slippage_pct"],
            "slippage_bps": int(C.SHADOW_SLIPPAGE_BPS) if shadow else slip_bps,
            "fees_sol": 0.0,
            "gas_sol": 0.0,
            "slippage_sol": 0.0,
            "fill_entry": fill_px,
            "tx_signature": None if shadow else live_meta.get("signature"),
            "max_float_pnl_pct": 0.0,
            "max_float_pnl_sol": 0.0,
            "realized_pnl_sol": 0.0,
        }
        self.cash -= sol
        self.positions[mint] = pos
        if shadow:
            # 影子：扣真实摩擦（DEX费+gas+可配置滑点），但不写实盘审计账本
            self._charge_friction(
                amount_sol=sol,
                side="buy",
                pos=pos,
                note="shadow_buy",
                slip_pct_override=shadow_slip_pct,
                write_ledger=False,
            )
        elif dry:
            self._charge_friction(amount_sol=sol, side="buy", pos=pos, note="buy")
        else:
            # 实盘摩擦已含在链上成交里；账本记一笔名义 gas 便于审计
            pos["gas_sol"] = float(pos.get("gas_sol") or 0) + 0.000005
            self.total_gas += 0.000005
            pump_ledger.append({
                "kind": "gas",
                "amount": 0.000005,
                "symbol": pos.get("symbol"),
                "position_id": pos.get("id"),
                "note": "live_buy",
                "meta": {"signature": live_meta.get("signature")},
            })
        self._persist_account()
        trade = journal.record_trade(
            action="buy",
            mint=mint,
            symbol=pos["symbol"],
            amount_sol=sol,
            price=mid,
            pnl_sol=None,
            pnl_percent=None,
            exit_reason="",
            dry_run=dry,
            shadow=shadow,
            metrics=_pos_metrics(pos, signal),
            position_id=pos["id"],
        )
        trade["fee_sol"] = pos["fees_sol"]
        trade["gas_sol"] = pos["gas_sol"]
        trade["slippage_sol"] = pos["slippage_sol"]
        trade["fill_price"] = fill_px
        trade["tx_signature"] = None if shadow else live_meta.get("signature")
        tag = "[SHADOW]" if shadow else ("[DRY]" if dry else "[LIVE]")
        logger.info(
            "%s OPEN %s @%.8g sol=%.4f slip_bps=%d sig=%s",
            tag,
            pos["symbol"],
            mid,
            sol,
            slip_bps,
            "virtual" if shadow else (live_meta.get("signature") or "—")[:12],
        )
        pos["last_trade"] = trade
        if shadow:
            shadow_report.note_open(pos)
        return pos

    def mark(self, mint: str, price: float) -> None:
        pos = self.positions.get(mint)
        if not pos or price <= 0:
            return
        pos["mark"] = price
        pos["peak"] = max(float(pos.get("peak") or 0), price)
        # 移动止盈线仅在 TP1 / 保本接管后启用；否则 UI 会误显示「回撤线」
        if pos.get("tp1_done") or pos.get("be_takeover"):
            pos["trail_line"] = float(pos["peak"]) * (1.0 - C.TRAIL_DRAWDOWN)
        entry = float(pos["entry"])
        pos["pnl_pct"] = (price - entry) / entry if entry else 0.0
        if pos.get("shadow") or self.shadow:
            shadow_report.note_mark(pos, price)

    def _close_partial(
        self, pos: dict[str, Any], ratio: float, price: float, reason: str
    ) -> dict[str, Any]:
        ratio = max(0.0, min(1.0, ratio))
        qty = float(pos["qty_left"]) * ratio
        if qty <= 0:
            return {}
        mid = float(price)
        entry = float(pos["entry"])
        shadow = bool(pos.get("shadow") or self.shadow or C.SHADOW_MODE)
        dry = True if shadow else bool(pos.get("dry_run", self.dry_run))
        live_meta: dict[str, Any] = {}
        slip_bps = risk_guard.clamp_slippage_bps(
            int(pos.get("slippage_bps") or C.MAX_SLIPPAGE_BPS)
        )

        # ---- 卖出前风控（影子跳过；平仓不受开仓熔断阻止）----
        if not shadow:
            try:
                risk_guard.pre_trade_gate(
                    side="sell",
                    equity=self.equity(),
                    cash=self.cash,
                    amount_sol=max(qty * mid, 1e-9),
                    slippage_bps=slip_bps,
                    stop_file=False,
                )
            except RiskBlocked as exc:
                logger.error("平仓被风控拦截: %s", exc)
                return {}

        if shadow:
            # 影子：真实盘口价虚拟卖出，不发链上交易
            proceeds = qty * mid
        elif not dry:
            from .live_swap import LiveSwapError, sell_token_for_sol

            try:
                decimals = int(pos.get("decimals") or 6)
                qty_raw_total = int(pos.get("qty_raw") or round(float(pos["qty"]) * (10 ** decimals)))
                raw_sell = max(1, int(round(qty_raw_total * ratio)))
                live_meta = sell_token_for_sol(
                    token_mint=pos["mint"],
                    token_amount_raw=raw_sell,
                    decimals=decimals,
                    slippage_bps=slip_bps,
                    equity=self.equity(),
                    approx_sol=qty * mid,
                )
                proceeds = float(live_meta.get("sol_amount") or (qty * mid))
                if live_meta.get("fill_price"):
                    mid = float(live_meta["fill_price"])
                # 同步剩余 raw
                pos["qty_raw"] = max(0, qty_raw_total - raw_sell)
            except (RiskBlocked, LiveSwapError, Exception) as exc:
                logger.error("LIVE 平仓失败（保留仓位）: %s", exc)
                return {}
        else:
            proceeds = qty * mid

        cost = qty * entry
        gross = proceeds - cost
        pnl_pct = ((mid - entry) / entry * 100.0) if entry > 0 else 0.0
        pos["qty_left"] = float(pos["qty_left"]) - qty

        if shadow:
            # 影子卖出：扣真实摩擦（滑点+费+gas），不写实盘审计账本
            shadow_slip_pct = float(C.SHADOW_SLIPPAGE_BPS) / 10_000.0
            costs = self._charge_friction(
                amount_sol=proceeds,
                side="sell",
                pos=pos,
                note=f"shadow_{reason}",
                slip_pct_override=shadow_slip_pct,
                write_ledger=False,
            )
            fill_px = AL.pump_fill_price(mid, side="sell", slip_pct=shadow_slip_pct)
        elif dry:
            costs = self._charge_friction(amount_sol=proceeds, side="sell", pos=pos, note=reason)
            fill_px = AL.pump_fill_price(mid, side="sell", slip_pct=costs["slippage_pct"])
        else:
            costs = {"fee_sol": 0.0, "gas_sol": 0.000005, "slippage_sol": 0.0, "slippage_pct": slip_bps / 10_000.0}
            self.cash -= costs["gas_sol"]
            self.total_gas += costs["gas_sol"]
            pos["gas_sol"] = float(pos.get("gas_sol") or 0) + costs["gas_sol"]
            fill_px = float(live_meta.get("fill_price") or mid)
            pump_ledger.append({
                "kind": "gas",
                "amount": costs["gas_sol"],
                "symbol": pos["symbol"],
                "position_id": pos.get("id"),
                "note": f"live_{reason}",
                "meta": {"signature": live_meta.get("signature")},
            })

        self.cash += proceeds
        self.gross_realized += gross
        if not shadow:
            pump_ledger.append({
                "kind": "gross_pnl",
                "amount": gross,
                "symbol": pos["symbol"],
                "position_id": pos.get("id"),
                "note": reason,
                "meta": {"mid": mid, "entry": entry, "qty": qty, "signature": live_meta.get("signature")},
            })
        self.realized_pnl = self.net_realized()
        self._persist_account()
        net = gross - costs["fee_sol"] - costs["gas_sol"] - costs["slippage_sol"]
        pos["realized_pnl_sol"] = float(pos.get("realized_pnl_sol") or 0) + net
        logger.info(
            "SETTLE %s %s gross=%+.6f fee=%.6f gas=%.6f slip=%.6f net=%+.6f equity=%.6f sig=%s",
            reason, pos["symbol"], gross, costs["fee_sol"], costs["gas_sol"],
            costs["slippage_sol"], net, self.equity(),
            "virtual" if shadow else (live_meta.get("signature") or "—")[:12],
        )
        trade = journal.record_trade(
            action=reason,
            mint=pos["mint"],
            symbol=pos["symbol"],
            amount_sol=proceeds,
            price=mid,
            pnl_sol=net,
            pnl_percent=pnl_pct,
            dry_run=dry,
            shadow=shadow,
            metrics=_pos_metrics(pos),
            position_id=pos.get("id"),
        )
        trade["gross_pnl_sol"] = round(gross, 8)
        trade["fee_sol"] = costs["fee_sol"]
        trade["gas_sol"] = costs["gas_sol"]
        trade["slippage_sol"] = costs["slippage_sol"]
        trade["fill_price"] = fill_px
        trade["tx_signature"] = None if shadow else live_meta.get("signature")
        trade["max_float_pnl_pct"] = pos.get("max_float_pnl_pct")
        if shadow:
            if float(pos.get("qty_left") or 0) > 1e-18:
                shadow_report.note_partial_close(pos, reason=reason, price=mid, pnl_sol=net)
            else:
                shadow_report.note_full_close(
                    pos,
                    reason=reason,
                    price=mid,
                    pnl_sol=float(pos.get("realized_pnl_sol") or net),
                    pnl_percent=(
                        float(pos.get("realized_pnl_sol") or net)
                        / max(float(pos.get("sol_spent") or C.SHADOW_SIZE_SOL), 1e-12)
                        * 100.0
                    ),
                )
        else:
            self.run_audit(auto_correct=True)
        return trade

    def manage(self, price_map: dict[str, float]) -> list[dict[str, Any]]:
        """出场管理（优先级从高到低）：
        1) 价格硬止损（momentum 默认 -13%）
        2) 时间止损（momentum 默认 12 分钟）——盈利豁免 + 保本接管：
           · 浮亏 / 僵尸震荡盘（pnl ≤ 0）：强制清仓释放资金；
           · 浮盈盘（pnl > 0）：取消时间止损，硬止损上移至保本价，
             全权交由移动止盈继续追踪，不再受时间约束。
        3) TP1（momentum 默认 +22% 卖 50%），剩余转入移动止盈
        4) 移动止盈 / 保本止损：从峰值回落触发（momentum 默认 9%）
        """
        events: list[dict[str, Any]] = []
        now = time.time()
        for mint, pos in list(self.positions.items()):
            px = price_map.get(mint)
            if px is None:
                px = float(pos.get("mark") or pos["entry"])
            self.mark(mint, px)
            age_m = (now - float(pos["opened_at"])) / 60.0
            entry = float(pos["entry"])
            pnl_pct = (px - entry) / entry if entry else 0.0

            # ① 价格硬止损（最高优先级）：浮亏 ≤ -25% → 立刻全仓斩仓
            if pnl_pct <= -float(C.HARD_STOP_PCT):
                trade = self._close_partial(pos, 1.0, px, "hard_stop")
                events.append(
                    {
                        "type": "hard_stop",
                        "symbol": pos["symbol"],
                        "mint": mint,
                        "price": px,
                        "pnl_pct": pnl_pct,
                        "age_m": age_m,
                        "trade": trade,
                    }
                )
                logger.error(
                    "🚨 HARD_STOP %s @%.8g (%.1f%%) age=%.1fm — 全仓斩仓",
                    pos["symbol"],
                    px,
                    pnl_pct * 100,
                    age_m,
                )
                self.positions.pop(mint, None)
                continue

            # ② 时间止损（满 25 分钟）：盈利豁免 + 保本接管（方案B）
            if age_m >= C.TIME_STOP_MINUTES and not pos.get("time_exempt"):
                if pnl_pct > 0:
                    # 浮盈盘：取消时间止损，硬止损上移至保本价，交移动止盈冲刺
                    pos["time_exempt"] = True
                    pos["be_takeover"] = True
                    pos["be_price"] = entry
                    pos["peak"] = max(float(pos.get("peak") or entry), px)
                    events.append(
                        {"type": "be_takeover", "symbol": pos["symbol"], "mint": mint,
                         "price": px, "pnl_pct": pnl_pct, "age_m": age_m}
                    )
                    logger.info(
                        "⏱️→🔒 TIME_EXEMPT %s age=%.1fm 浮盈+%.1f%% — 时间止损失效、硬止损上移保本、转移动止盈",
                        pos["symbol"], age_m, pnl_pct * 100,
                    )
                    # 不平仓，继续走后续保护逻辑
                else:
                    # 浮亏 / 僵尸震荡盘：强制清仓释放资金
                    trade = self._close_partial(pos, 1.0, px, "time_stop")
                    events.append({"type": "time_stop", "symbol": pos["symbol"], "mint": mint, "price": px, "age_m": age_m, "pnl_pct": pnl_pct, "trade": trade})
                    logger.info("TIME_STOP %s after %.1fm (pnl=%.1f%%)", pos["symbol"], age_m, pnl_pct * 100)
                    self.positions.pop(mint, None)
                    continue

            # ③ 第一止盈 TP1：+18% 卖出 55%（保本接管单跳过，整仓交移动止盈追踪）
            if not pos.get("tp1_done") and not pos.get("be_takeover") and pnl_pct >= C.TP1_PCT:
                trade = self._close_partial(pos, C.TP1_SELL_RATIO, px, "tp1")
                pos["tp1_done"] = True
                pos["peak"] = px
                pos["trail_line"] = px * (1.0 - C.TRAIL_DRAWDOWN)
                events.append(
                    {"type": "tp1", "symbol": pos["symbol"], "mint": mint, "price": px, "pnl_pct": pnl_pct, "trade": trade}
                )
                logger.info("TP1 %s @%.8g (+%.1f%%)", pos["symbol"], px, pnl_pct * 100)

            # ④ 移动止盈 / 保本止损：TP1 后 或 保本接管后，从峰值回落触发
            if pos.get("tp1_done") or pos.get("be_takeover"):
                trail_line = float(pos.get("trail_line") or 0)
                if pos.get("be_takeover") and not pos.get("tp1_done"):
                    # 保本接管：保护线取「保本价」与「移动止盈线」的高者
                    be_floor = float(pos.get("be_price") or entry)
                    eff_line = max(trail_line, be_floor)
                    exit_reason = "be_stop"
                else:
                    eff_line = trail_line
                    exit_reason = "trail_stop"
                if eff_line > 0 and px <= eff_line:
                    trade = self._close_partial(pos, 1.0, px, exit_reason)
                    events.append({"type": exit_reason, "symbol": pos["symbol"], "mint": mint, "price": px, "pnl_pct": pnl_pct, "trade": trade})
                    logger.info("%s %s @%.8g line=%.8g (%.1f%%)", exit_reason.upper(), pos["symbol"], px, eff_line, pnl_pct * 100)
                    self.positions.pop(mint, None)
                    continue

            if float(pos.get("qty_left") or 0) <= 1e-18:
                self.positions.pop(mint, None)

        return events

    def snapshot_positions(self) -> list[dict[str, Any]]:
        rows = []
        for pos in self.positions.values():
            mark = float(pos.get("mark") or pos["entry"])
            entry = float(pos["entry"])
            qty_left = float(pos.get("qty_left") or 0)
            qty = float(pos.get("qty") or 0) or 1e-18
            sol_spent = float(pos.get("sol_spent") or 0)
            ratio = qty_left / qty if qty > 0 else 0.0
            # 仓值 = 剩余代币 * 现价；成本按剩余仓位比例摊
            position_value_sol = qty_left * mark
            cost_sol = sol_spent * ratio
            unrealized_sol = position_value_sol - cost_sol
            pnl_pct = ((mark - entry) / entry * 100.0) if entry > 0 else 0.0
            rows.append(
                {
                    "id": pos["id"],
                    "mint": pos["mint"],
                    "symbol": pos["symbol"],
                    "entry": entry,
                    "mark": mark,
                    "current_price": mark,
                    "entry_price": entry,
                    # 原样浮点 + 高精度字符串，避免前端二次抹平
                    "entry_repr": f"{entry:.18g}",
                    "mark_repr": f"{mark:.18g}",
                    "pnl_pct": round(pnl_pct, 4),
                    "qty_left": qty_left,
                    "qty": qty,
                    "position_value_sol": round(position_value_sol, 6),
                    "cost_sol": round(cost_sol, 6),
                    "unrealized_pnl_sol": round(unrealized_sol, 6),
                    "tp1_done": bool(pos.get("tp1_done")),
                    "be_takeover": bool(pos.get("be_takeover")),
                    "time_exempt": bool(pos.get("time_exempt")),
                    "trail_line": pos.get("trail_line"),
                    "qty_left_ratio": round(ratio, 3),
                    "age_minutes": round((time.time() - float(pos["opened_at"])) / 60.0, 1),
                    "dry_run": pos.get("dry_run"),
                    "shadow": bool(pos.get("shadow")),
                    "pool": pos.get("pool"),
                    "dex": pos.get("dex"),
                    "price_source": pos.get("price_source"),
                    "price_ts": pos.get("price_ts"),
                    "max_float_pnl_pct": pos.get("max_float_pnl_pct"),
                    "sol_spent": pos.get("sol_spent"),
                    "fees_sol": pos.get("fees_sol"),
                    "gas_sol": pos.get("gas_sol"),
                    "slippage_sol": pos.get("slippage_sol"),
                }
            )
        return rows

    def unrealized_pnl(self) -> float:
        total = 0.0
        for pos in self.positions.values():
            mark = float(pos.get("mark") or pos["entry"])
            entry = float(pos["entry"])
            total += (mark - entry) * float(pos["qty_left"])
        return total

    def position_value(self) -> float:
        return sum(
            float(pos["qty_left"]) * float(pos.get("mark") or pos["entry"])
            for pos in self.positions.values()
        )

    def equity(self) -> float:
        """运营权益 = 现金 + 在仓市值。"""
        return self.cash + self.position_value()

    def equity_from_ledger(self) -> float:
        return AL.expected_equity(
            initial=self.bankroll,
            gross_realized=self.gross_realized,
            fees=self.total_fees,
            slippage=self.total_slippage,
            funding=0.0,
            unrealized=self.unrealized_pnl(),
            gas=self.total_gas,
        )

    def reset_live_session(self, sol_balance: float) -> None:
        """实盘会话重置：切断纸面账本，只用链上余额作本金/现金。"""
        bal = max(0.0, float(sol_balance))
        self.bankroll = bal
        self.cash = bal
        self.gross_realized = 0.0
        self.total_fees = 0.0
        self.total_slippage = 0.0
        self.total_gas = 0.0
        self.realized_pnl = 0.0
        self.positions.clear()
        self.last_audit = {
            "ok": True,
            "skipped": True,
            "reason": "live_session_reset",
        }
        self._persist_account()
        logger.info("LIVE 会话账户已重置 bankroll=cash=%.6f（纸面盈亏已清零）", bal)

    def sync_live_balance(self, sol_balance: float) -> None:
        """实盘空仓时，现金/权益强制对齐链上余额，避免纸面回写。"""
        if self.dry_run:
            return
        bal = max(0.0, float(sol_balance))
        if self.positions:
            # 有持仓时保留仓位市值，现金部分用「链上余额 − 估算仓位」不精确，暂不覆盖
            return
        # 空仓：权益应等于链上 SOL
        self.cash = bal
        # 已实现 = 相对本金的链上变化（本金在 live_bankroll / bankroll）
        self.realized_pnl = bal - float(self.bankroll)
        self.gross_realized = self.realized_pnl
        self.total_fees = 0.0
        self.total_slippage = 0.0
        self.total_gas = 0.0

    def run_audit(self, *, auto_correct: bool = True) -> dict[str, Any]:
        # 实盘禁止用纸面复式账本自动改写账户（那是假的 +0.7 污染源）
        if not self.dry_run:
            result = {
                "ok": True,
                "skipped": True,
                "reason": "live_mode_skip_paper_ledger_audit",
                "displayed_equity": self.equity(),
                "displayed_realized_net": self.net_realized(),
            }
            self.last_audit = result
            return result

        result = AL.run_audit_check(
            pump_ledger,
            initial=self.bankroll,
            displayed_equity=self.equity(),
            displayed_realized_net=self.net_realized(),
            unrealized=self.unrealized_pnl(),
            auto_correct=auto_correct,
        )
        if (not result["ok"]) and auto_correct and result.get("correction"):
            sums = pump_ledger.sum_costs()
            self.gross_realized = sums["gross_realized"]
            self.total_fees = sums["fees"]
            self.total_slippage = sums["slippage"]
            self.total_gas = sums["gas"]
            self.realized_pnl = sums["net_realized"]
            # 现金 = 本金 + 净实现 - 在仓成本（sol_spent * qty_left/qty）
            locked = 0.0
            for pos in self.positions.values():
                q = float(pos["qty"]) or 1.0
                locked += float(pos["sol_spent"]) * (float(pos["qty_left"]) / q)
            self.cash = self.bankroll + self.net_realized() - locked
            self._persist_account()
            result["corrected"] = True
            result["equity_after"] = self.equity()
            logger.warning("[AUDIT] Pump 账目已按复式账本修正 equity=%.6f", self.equity())
        self.last_audit = result
        return result

    def audit_report_24h(self) -> dict[str, Any]:
        sums = pump_ledger.sum_costs(hours=24.0)
        return AL.build_24h_audit_report(
            pump_ledger,
            initial=self.bankroll,
            displayed_equity=self.equity(),
            unrealized=self.unrealized_pnl(),
            extra={
                "total_fees_sol": sums["fees"],
                "total_slippage_sol": sums["slippage"],
                "total_gas_sol": sums["gas"],
            },
        )
