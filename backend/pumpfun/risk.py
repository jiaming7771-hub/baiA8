"""实盘硬风控：滑点 / 仓位 / 回撤熔断 — 买入卖出前必须全部通过。"""

from __future__ import annotations

import logging
import time
from typing import Any

from . import config as C

logger = logging.getLogger("pumpfun.risk")


class RiskBlocked(RuntimeError):
    """风控拦截：禁止继续开仓或下单。"""


class RiskGuard:
    """进程内风控状态机。"""

    def __init__(self) -> None:
        self.peak_equity: float | None = None
        self.drawdown_halted = False
        self.halt_reason: str | None = None
        self.halted_at: float | None = None
        self.last_check: dict[str, Any] = {}

    def reset_halt(self) -> None:
        self.drawdown_halted = False
        self.halt_reason = None
        self.halted_at = None
        logger.warning("风控熔断已人工解除（peak 保留）")

    def clamp_slippage_bps(
        self, requested: int | None = None, *, urgent: bool = False
    ) -> int:
        """常规夹紧到 [HARD_MIN, 10%]；urgent 逃生可抬到 URGENT_SLIPPAGE_BPS_MAX（默认 30%）。

        HARD_MIN 现为 100bps：入场可走 ENTRY_MAX_SLIPPAGE_BPS（默认 250），
        出场仍可用 MAX_SLIPPAGE_BPS（默认 500）。旧 HARD_MIN=500 会把入场硬抬到 5%。
        """
        raw = int(requested if requested is not None else C.MAX_SLIPPAGE_BPS)
        hard_max = (
            int(C.URGENT_SLIPPAGE_BPS_MAX) if urgent else int(C.SLIPPAGE_BPS_HARD_MAX)
        )
        hard_min = int(C.SLIPPAGE_BPS_HARD_MIN)
        clamped = max(hard_min, min(raw, hard_max))
        if clamped != raw:
            logger.warning(
                "滑点 bps 被夹紧 %s → %s（urgent=%s max=%s）",
                raw,
                clamped,
                urgent,
                hard_max,
            )
        return clamped

    def clamp_position_sol(self, raw_sol: float, *, equity: float, cash: float) -> float:
        """单笔仓位：权益 1%~2%，且夹在 0.02~0.04 SOL，且不超过可用现金。

        Micro-Live：固定小额 LIVE_SIZE_SOL（硬顶 0.10 SOL），跳过百分比 sizing，
        但仍受现金余额约束（留 10% 余量付 gas/优先费）。
        """
        if C.MICRO_LIVE:
            # 默认固定 LIVE_SIZE；允许调用方传入更小试水仓（如早轨半仓），
            # 但仍夹在 [HARD_MIN, LIVE_SIZE] 内，绝不放大超过配置单笔。
            base = max(
                C.LIVE_SIZE_SOL_HARD_MIN,
                min(float(C.LIVE_SIZE_SOL), C.LIVE_SIZE_SOL_HARD_MAX),
            )
            raw = float(raw_sol) if raw_sol and raw_sol > 0 else base
            sized = max(C.LIVE_SIZE_SOL_HARD_MIN, min(raw, base))
            available = max(0.0, float(cash) * 0.90)
            if sized > available + 1e-12:
                raise RiskBlocked(
                    f"Micro-Live 现金不足：size={sized:.4f} > 可用 {available:.4f} SOL"
                )
            return round(sized, 8)
        pct = max(C.POSITION_PCT_HARD_MIN, min(C.POSITION_PCT, C.POSITION_PCT_HARD_MAX))
        by_pct = float(equity) * pct
        sized = max(C.MIN_POSITION_SOL, min(by_pct, C.MAX_POSITION_SOL))
        # 再与请求值、现金取小
        sized = min(sized, float(raw_sol) if raw_sol > 0 else sized)
        sized = min(sized, max(0.0, float(cash) * 0.90))
        if sized + 1e-12 < C.MIN_POSITION_SOL:
            raise RiskBlocked(
                f"仓位过小或现金不足：sized={sized:.6f} < min={C.MIN_POSITION_SOL}"
            )
        if sized > C.MAX_POSITION_SOL + 1e-12:
            raise RiskBlocked(f"仓位超过硬顶 {C.MAX_POSITION_SOL} SOL")
        return round(sized, 8)

    def update_equity(self, equity: float) -> dict[str, Any]:
        """更新峰值并判定总亏损熔断（回撤≥15% 或绝对亏损≥0.6 SOL）。

        熔断后禁止一切新开仓；已有持仓允许正常平仓逃生。
        """
        eq = float(equity)
        if self.peak_equity is None or eq > self.peak_equity:
            self.peak_equity = eq
        peak = float(self.peak_equity or eq)
        dd = 0.0 if peak <= 0 else max(0.0, (peak - eq) / peak)
        abs_loss = max(0.0, peak - eq)
        trip_dd = dd >= float(C.DRAWDOWN_HALT)
        trip_abs = abs_loss >= float(C.ABS_LOSS_HALT_SOL)
        if (trip_dd or trip_abs) and not self.drawdown_halted:
            self.drawdown_halted = True
            reasons = []
            if trip_dd:
                reasons.append(f"回撤 {dd:.2%} ≥ {C.DRAWDOWN_HALT:.2%}")
            if trip_abs:
                reasons.append(
                    f"绝对亏损 {abs_loss:.4f} SOL ≥ {C.ABS_LOSS_HALT_SOL:.2f} SOL"
                )
            self.halt_reason = (
                f"总亏损熔断：{' / '.join(reasons)} "
                f"(equity={eq:.6f} peak={peak:.6f})"
            )
            self.halted_at = time.time()
            logger.error(
                "🚨🚨🚨 %s — 将写入 STOP.txt 并停止一切新开仓（允许已有仓位平仓逃生）",
                self.halt_reason,
            )
        info = {
            "equity": round(eq, 8),
            "peak_equity": round(peak, 8),
            "drawdown": round(dd, 6),
            "abs_loss_sol": round(abs_loss, 6),
            "drawdown_halt": float(C.DRAWDOWN_HALT),
            "abs_loss_halt_sol": float(C.ABS_LOSS_HALT_SOL),
            "halted": self.drawdown_halted,
            "halt_reason": self.halt_reason,
        }
        self.last_check = info
        return info

    def assert_can_open(self, *, equity: float, stop_file: bool = False) -> None:
        self.update_equity(equity)
        if stop_file:
            raise RiskBlocked("STOP.txt 熔断生效，禁止开仓")
        if self.drawdown_halted:
            raise RiskBlocked(self.halt_reason or "回撤熔断中，禁止开仓")

    def assert_slippage_ok(self, slippage_bps: int) -> int:
        bps = self.clamp_slippage_bps(slippage_bps)
        if bps > C.SLIPPAGE_BPS_HARD_MAX:
            raise RiskBlocked(f"滑点 {bps}bps 超过绝对硬顶 {C.SLIPPAGE_BPS_HARD_MAX}")
        return bps

    def pre_trade_gate(
        self,
        *,
        side: str,
        equity: float,
        cash: float,
        amount_sol: float,
        slippage_bps: int | None = None,
        stop_file: bool = False,
    ) -> dict[str, Any]:
        """买入/卖出前统一硬拦截。返回夹紧后的参数。"""
        side_l = (side or "").lower()
        if side_l in ("buy", "open", "long"):
            self.assert_can_open(equity=equity, stop_file=stop_file)
        else:
            # 卖出仍更新权益，但不因熔断阻止平仓（保命出场）
            self.update_equity(equity)

        bps = self.assert_slippage_ok(
            int(slippage_bps if slippage_bps is not None else C.MAX_SLIPPAGE_BPS)
        )
        if side_l in ("buy", "open", "long"):
            sol = self.clamp_position_sol(amount_sol, equity=equity, cash=cash)
        else:
            # 卖出：不允许把「开仓上限」当成卖出金额限制；仅校验为正
            sol = float(amount_sol)
            if sol <= 0:
                raise RiskBlocked("卖出金额无效")

        result = {
            "side": side_l,
            "amount_sol": sol,
            "slippage_bps": bps,
            "max_slippage_pct": bps / 100.0,
            "position_pct": C.POSITION_PCT,
            "risk": dict(self.last_check),
        }
        logger.info(
            "风控放行 side=%s sol=%.6f slip=%dbps(%s%%) dd=%s halted=%s",
            side_l,
            sol,
            bps,
            bps / 100.0,
            (self.last_check or {}).get("drawdown"),
            self.drawdown_halted,
        )
        return result

    def snapshot(self) -> dict[str, Any]:
        return {
            "peak_equity": self.peak_equity,
            "drawdown_halted": self.drawdown_halted,
            "halt_reason": self.halt_reason,
            "halted_at": self.halted_at,
            "last_check": self.last_check,
            "limits": {
                "max_slippage_bps": C.MAX_SLIPPAGE_BPS,
                "slippage_hard_max_bps": C.SLIPPAGE_BPS_HARD_MAX,
                "position_pct": C.POSITION_PCT,
                "min_pos_sol": C.MIN_POSITION_SOL,
                "max_pos_sol": C.MAX_POSITION_SOL,
                "drawdown_halt": C.DRAWDOWN_HALT,
                "abs_loss_halt_sol": C.ABS_LOSS_HALT_SOL,
                "hard_stop_pct": C.HARD_STOP_PCT,
                "tp1_pct": C.TP1_PCT,
                "trail_dd": C.TRAIL_DRAWDOWN,
                "time_stop_m": C.TIME_STOP_MINUTES,
            },
        }


# 进程单例
guard = RiskGuard()
