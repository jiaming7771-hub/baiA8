"""模拟下单与止损/止盈生命周期。"""

from __future__ import annotations

import json
import logging
import threading
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


def _exit_params(pos: dict[str, Any] | None = None) -> dict[str, float]:
    """按持仓轨道返回出场参数；默认 A。"""
    track = (pos or {}).get("track") or "A"
    if track == "B":
        return {
            "hard_stop": float(C.TRACK_B_HARD_STOP),
            "tp1": float(C.TRACK_B_TP1),
            "tp1_sell": float(C.TRACK_B_TP1_SELL),
            "trail": float(C.TRACK_B_TRAIL),
            "time_stop": float(C.TRACK_B_TIME_STOP),
        }
    return {
        "hard_stop": float(C.TRACK_A_HARD_STOP),
        "tp1": float(C.TRACK_A_TP1),
        "tp1_sell": float(C.TRACK_A_TP1_SELL),
        "trail": float(C.TRACK_A_TRAIL),
        "time_stop": float(C.TRACK_A_TIME_STOP),
    }


def _fired_threshold(pos: dict[str, Any], reason: str) -> tuple[float | None, float | None]:
    """返回 (触发阈值, 卖出比例)——按**这个仓位当时**的轨道参数取，供日志冻结。

    日志标签必须写这两个数，不能让复盘时的配置去渲染历史（同一份历史里
    hard_stop 出现过 -13%/-25%/-35%）。崩塌止损走的是 PANIC_STOP_PCT，
    与轨道硬止损不是一个数，故 manage() 会在开火时把它写进 stop_fired_threshold。
    """
    xp = _exit_params(pos)
    if reason == "tp1":
        return xp["tp1"], xp["tp1_sell"]
    if reason == "hard_stop":
        fired = pos.get("stop_fired_threshold")
        return (float(fired) if fired is not None else xp["hard_stop"]), None
    if reason in ("trail_stop", "be_stop"):
        return xp["trail"], None
    if reason == "time_stop":
        return xp["time_stop"], None
    return None, None


def mark_basis(pos: dict[str, Any]) -> float:
    """管仓盈亏的基准价，必须与后续 mark **同源同口径**。

    优先 entry_mark（成交后立刻读的链上池价），退回 entry（Jupiter 成交价）。
    出场阶梯（TP1/移动止盈/硬止损）全部拿它算；账本结算仍用 entry（真金白银）。
    """
    basis = float(pos.get("entry_mark") or 0)
    if basis > 0:
        return basis
    return float(pos.get("entry") or 0)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _entry_gate_snapshot(track: str | None) -> dict[str, Any]:
    """开这一笔时真正生效的准入线。跟 scoring 一起落盘：分数要能跟当时的门槛
    对照，才知道「45 分没买」是分低还是门槛高。track 决定用哪套硬过滤阈值。"""
    return {
        "min_score": float(C.ENTRY_MIN_SCORE),
        "ath_drop_max": float(C.ENTRY_ATH_DROP_MAX),
        "graduated_only": bool(C.ENTRY_GRADUATED_ONLY),
        "bonding_min_pct": float(C.BONDING_MIN_PROGRESS_PCT),
        "track": track or "A",
    }


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
        "min_float_pnl_pct": pos.get("min_float_pnl_pct"),
        # 浮盈亏极值算在哪个基准上：entry_mark（链上标价，与出场阶梯同源）还是
        # 退回的成交价。缺了这个，极值就没法跟同记录里的 pnl_percent（成交价口径）
        # 对照，两个数会被当成同一把尺子。
        "float_basis": (
            "entry_mark" if float(pos.get("entry_mark") or 0) > 0 else "fill"
        ),
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
        # 实盘本金锚点（跨重启保留，避免收益率被清零）
        self.live_bankroll_anchor: float | None = None
        # 最近一次真实成交的 monotonic 时间：链上余额同步据此判断读数是否已过期
        self._last_fill_mono = 0.0
        # 开仓竞态：扫描→审计→Jupiter 之间防双开；manage 写仓也走同一把锁
        self._trade_lock = threading.RLock()
        self._opening: set[str] = set()
        # mint → unix ts：whale_dump / 同类熔断后冷却，禁止立刻再开
        self._mint_cooldown_until: dict[str, float] = {}
        # 强反转提前解锁所需：冷却起始时间 / 上次开仓价 / 已用重试次数
        self._cooldown_armed_at: dict[str, float] = {}
        self._last_exit_entry: dict[str, float] = {}
        self._reentry_used: dict[str, int] = {}
        # 同 mint 亏损硬封禁（强反转不可解）
        self._mint_loss_bans: dict[str, dict[str, Any]] = {}
        self._load_mint_loss_bans()
        # 同名 Symbol 冷却（防换 mint 连环开同一 ticker）
        self._symbol_cooldown_until: dict[str, float] = {}
        self._load_symbol_cooldowns()
        # 开发者/部署者画像（连环发盘 + 亏损封禁；治换 mint 换名同一 creator）
        self._creator_stats: dict[str, dict[str, Any]] = {}
        self._load_creator_stats()
        self._seed_cooldown_from_recent_dumps()
        self._seed_loss_bans_from_recent_trades()
        self._seed_symbol_cooldowns_from_trades()
        self._restore_account()
        self._restore_positions()
        if self.shadow:
            # 影子模式：虚拟本金；盈亏必须与 account / shadow_trades 对齐，禁止启动清零
            self.bankroll = max(
                float(self.bankroll or 0),
                float(C.BANKROLL_SOL),
                float(C.SHADOW_SIZE_SOL) * 10,
            )
            shadow_net = float(shadow_report.lifetime_net_pnl())
            acct_net = float(self.net_realized())
            # 账户空但影子日志有成交 → 从日志重建，避免刷新/重启后收益率归零
            if abs(acct_net) < 1e-9 and abs(shadow_net) > 1e-9:
                self.gross_realized = shadow_net
                self.total_fees = 0.0
                self.total_slippage = 0.0
                self.total_gas = 0.0
                self.realized_pnl = shadow_net
                logger.warning(
                    "👻 影子盈亏已从 shadow_trades 重建 net=%+.6f SOL（账户文件为空）",
                    shadow_net,
                )
            elif abs(shadow_net - acct_net) > 0.05:
                # 日志与账户偏差过大时以影子日志为准（看板/报告同源）
                self.gross_realized = shadow_net
                self.total_fees = 0.0
                self.total_slippage = 0.0
                self.total_gas = 0.0
                self.realized_pnl = shadow_net
                logger.warning(
                    "👻 影子账户与日志偏差 %.4f → 以日志为准 net=%+.6f",
                    shadow_net - acct_net,
                    shadow_net,
                )
            if not self.positions:
                self.cash = self.bankroll + self.net_realized()
            self._persist_account()
            logger.warning(
                "👻 SHADOW_MODE=ON · 真行情喂价 · 虚拟成交（禁用 Jupiter）· 单笔名义 %.2f SOL · 权益基准 cash=%.4f net=%+.4f",
                C.SHADOW_SIZE_SOL,
                self.cash,
                self.net_realized(),
            )

    def net_realized(self) -> float:
        return self.gross_realized - self.total_fees - self.total_slippage - self.total_gas

    # ---------- 账户持久化 ----------
    def _restore_account(self) -> None:
        """优先读账户文件；缺失时用历史成交/账本重建。"""
        try:
            if C.ACCOUNT_FILE.exists():
                saved = json.loads(C.ACCOUNT_FILE.read_text(encoding="utf-8"))
                anchor = saved.get("live_bankroll_sol")
                if anchor is not None and float(anchor) > 0:
                    self.live_bankroll_anchor = float(anchor)
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

    def _arm_mint_cooldown(
        self,
        mint: str,
        *,
        seconds: float | None = None,
        reason: str = "",
        entry_ref: float | None = None,
    ) -> None:
        """斩仓/止损后冷却，禁止立刻再开同一 mint。"""
        cool = float(seconds if seconds is not None else C.EXIT_COOLDOWN_SEC)
        if cool <= 0 or not mint:
            return
        until = time.time() + cool
        self._cooldown_armed_at[mint] = time.time()
        if entry_ref and float(entry_ref) > 0:
            self._last_exit_entry[mint] = float(entry_ref)
        prev = float(self._mint_cooldown_until.get(mint) or 0)
        if until > prev:
            self._mint_cooldown_until[mint] = until
            logger.info(
                "mint 冷却 %.0fs %s… reason=%s",
                cool,
                mint[:8],
                reason or "exit",
            )

    def _strong_reversal_unlock(self, mint: str, signal: dict[str, Any]) -> bool:
        """止损后强反转提前解锁冷却：需站回上次买入价 + 动量转强，每 mint 限次。"""
        if int(C.REENTRY_MAX_RETRY) <= 0:
            return False
        if int(self._reentry_used.get(mint) or 0) >= int(C.REENTRY_MAX_RETRY):
            return False
        armed = float(self._cooldown_armed_at.get(mint) or 0)
        if armed <= 0 or (time.time() - armed) < float(C.REENTRY_STRONG_SEC):
            return False
        ref = float(self._last_exit_entry.get(mint) or 0)
        px = float(signal.get("price") or 0)
        if ref <= 0 or px <= 0 or px < ref:
            return False
        if float(signal.get("chg_m5") or 0) <= 0:
            return False
        bs = float(signal.get("buy_sell_ratio") or 0)
        if bs > 0 and bs < float(C.BUY_SELL_RATIO_MIN):
            return False
        self._reentry_used[mint] = int(self._reentry_used.get(mint) or 0) + 1
        self._mint_cooldown_until.pop(mint, None)
        logger.warning(
            "🔓 强反转解锁 %s：现价 %.8g ≥ 上次买入 %.8g，冷却提前解除（该 mint 第 %d/%d 次）",
            signal.get("symbol") or mint[:6],
            px,
            ref,
            self._reentry_used[mint],
            int(C.REENTRY_MAX_RETRY),
        )
        return True

    def _read_entry_mark(
        self, mint: str, signal: dict[str, Any], *, fill_ref: float
    ) -> float:
        """成交后按「标价口径」再读一次链上现价，作为管仓盈亏基准。

        返回 0 表示读不到 / 明显不可信（此时管仓退回用成交价，行为与旧版一致）。
        只接受落在成交价 [1/ENTRY_MARK_MAX_GAP, ENTRY_MARK_MAX_GAP] 内的读数：
        真实基差只有手续费+滑点量级，偏离一倍以上说明读的是别的池或残池假价，
        拿它当基准会把止损线整体挪走，比不用更危险。
        """
        if fill_ref <= 0:
            return 0.0
        span = max(1.01, float(C.ENTRY_MARK_MAX_GAP))
        try:
            from .onchain_price import fetch_pool_price_sol

            row = fetch_pool_price_sol(mint, pool=signal.get("pool"), dex=signal.get("dex"))
        except Exception:
            logger.warning("开仓后读标价基准失败 %s（退回成交价）", mint[:8])
            return 0.0
        px = float((row or {}).get("price") or 0)
        if px <= 0 or (row or {}).get("vault_drained"):
            return 0.0
        if not (fill_ref / span <= px <= fill_ref * span):
            logger.error(
                "标价基准可疑 %s fill=%.8g chain=%.8g（偏离 %+.1f%% 超 ±%.0f%%）"
                "— 不采信，管仓退回成交价基准",
                signal.get("symbol") or mint[:6],
                fill_ref,
                px,
                (px - fill_ref) / fill_ref * 100,
                (span - 1) * 100,
            )
            return 0.0
        if abs(px - fill_ref) / fill_ref >= 0.02:
            logger.info(
                "标价基准 %s fill=%.8g chain=%.8g gap=%+.2f%% — 管仓改用链上基准",
                signal.get("symbol") or mint[:6],
                fill_ref,
                px,
                (px - fill_ref) / fill_ref * 100,
            )
        return px

    def _confirm_entry_price(
        self, signal: dict[str, Any], mid: float
    ) -> tuple[bool, float]:
        """买前短时确认：观察数秒，价格落在 [-drop, +rise] 窄带内才下单。

        返回 (是否放行, 最新价)。涨太多（追高）与跌太多（接刀）都否决。
        """
        wait = float(C.ENTRY_CONFIRM_SEC)
        if wait <= 0 or mid <= 0:
            return True, mid

        from .onchain_price import fetch_pool_price_sol

        mint = signal["mint"]
        sym = signal.get("symbol") or mint[:6]
        step = 2.0
        waited = 0.0
        low = mid
        high = mid
        last = mid
        samples: list[float] = []
        while waited < wait:
            nap = min(step, wait - waited)
            time.sleep(nap)
            waited += nap
            try:
                meta = fetch_pool_price_sol(
                    mint, pool=signal.get("pool"), dex=signal.get("dex")
                )
            except Exception:
                meta = None
            px = float((meta or {}).get("price") or 0)
            if px <= 0:
                continue
            last = px
            low = min(low, px)
            high = max(high, px)
            samples.append(px)

        # 微观结构确认：拒绝"单针假拉"。要求窗口内价格在起点上方站住≥N次报价，
        # 而不是只靠最后一笔冲上来（那种买完立刻回落 → 秒浮亏）。
        if C.ENTRY_FLOW_CONFIRM and len(samples) >= C.ENTRY_FLOW_MIN_HOLD_TICKS:
            hold = sum(1 for p in samples if p >= mid * 0.999)
            if hold < int(C.ENTRY_FLOW_MIN_HOLD_TICKS):
                logger.info(
                    "开仓确认失败 %s：%.0fs 内仅 %d/%d 次站上起点（疑似单针假拉）",
                    sym,
                    wait,
                    hold,
                    len(samples),
                )
                return False, last

        drop = (last - mid) / mid
        max_drop = float(C.ENTRY_CONFIRM_MAX_DROP)
        max_rise = float(C.ENTRY_CONFIRM_MAX_RISE)
        if drop <= -max_drop:
            logger.info(
                "开仓确认失败 %s：%.0fs 内 %+.1f%%（跌破 -%.0f%%）— 放弃接刀",
                sym,
                wait,
                drop * 100,
                max_drop * 100,
            )
            return False, last
        if drop >= max_rise:
            logger.info(
                "开仓确认失败 %s：%.0fs 内 %+.1f%%（涨破 +%.0f%%）— 放弃追高",
                sym,
                wait,
                drop * 100,
                max_rise * 100,
            )
            return False, last
        # 收在观察窗最低点附近且低于起点 → 仍在阴跌，不接刀
        if last < mid and last <= low * 1.001:
            logger.info(
                "开仓确认失败 %s：%.0fs 内持续走低 %.8g → %.8g（未止跌）",
                sym,
                wait,
                mid,
                last,
            )
            return False, last
        logger.info(
            "开仓确认通过 %s：%.0fs 后 %+.1f%%（%.8g → %.8g，窗内[%.8g,%.8g]）",
            sym,
            wait,
            drop * 100,
            mid,
            last,
            low,
            high,
        )
        return True, last

    def _seed_cooldown_from_recent_dumps(self) -> None:
        """重启后把近期止损/熔断的 mint 继续冷却，防连环再开。"""
        try:
            if not C.TRADES_FILE.exists():
                return
            cool = max(float(C.EXIT_COOLDOWN_SEC), float(C.EARLY_WHALE_COOLDOWN_SEC))
            cutoff = time.time() - cool
            block_actions = {
                "whale_dump",
                "hard_stop",
                "early_fade",
                "time_stop",
                "dead_stop",
                "trail_stop",
                "be_stop",
            }
            for line in C.TRADES_FILE.read_text(encoding="utf-8").splitlines()[-300:]:
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get("action") not in block_actions:
                    continue
                mint = row.get("mint")
                if not mint:
                    continue
                ts_raw = row.get("timestamp") or row.get("ts")
                try:
                    if isinstance(ts_raw, (int, float)):
                        ts = float(ts_raw)
                    else:
                        from datetime import datetime

                        ts = datetime.fromisoformat(
                            str(ts_raw).replace("Z", "+00:00")
                        ).timestamp()
                except Exception:
                    continue
                if ts >= cutoff:
                    until = ts + cool
                    prev = float(self._mint_cooldown_until.get(mint) or 0)
                    if until > prev:
                        self._mint_cooldown_until[mint] = until
            if self._mint_cooldown_until:
                logger.info(
                    "已恢复出场冷却 %d 个 mint（最长剩余 %.0fs）",
                    len(self._mint_cooldown_until),
                    max(0.0, max(self._mint_cooldown_until.values()) - time.time()),
                )
        except Exception:
            logger.exception("恢复出场冷却失败（忽略）")

    def _utc_day(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _load_mint_loss_bans(self) -> None:
        try:
            path = C.MINT_LOSS_BAN_FILE
            if not path.exists():
                return
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._mint_loss_bans = {
                    str(k): dict(v) for k, v in raw.items() if isinstance(v, dict)
                }
        except Exception:
            logger.exception("加载 mint 亏损封禁失败（忽略）")

    def _persist_mint_loss_bans(self) -> None:
        try:
            C.DATA_DIR.mkdir(parents=True, exist_ok=True)
            path = C.MINT_LOSS_BAN_FILE
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self._mint_loss_bans, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(path)
        except Exception:
            logger.exception("落盘 mint 亏损封禁失败")

    def _mint_loss_ban_remaining(self, mint: str) -> float:
        ent = self._mint_loss_bans.get(mint) or {}
        until = float(ent.get("ban_until") or 0)
        return max(0.0, until - time.time())

    def _record_mint_loss(self, mint: str, *, reason: str, pnl_sol: float | None = None) -> None:
        """亏损出场 → 累加当日次数并硬封禁（强反转不可解）。"""
        if not mint:
            return
        # 盈利出场不记
        if pnl_sol is not None and float(pnl_sol) >= 0:
            return
        day = self._utc_day()
        ent = dict(self._mint_loss_bans.get(mint) or {})
        if ent.get("day") != day:
            ent = {"day": day, "losses": 0, "ban_until": 0.0}
        losses = int(ent.get("losses") or 0) + 1
        ent["losses"] = losses
        ent["last_reason"] = reason
        ban1 = float(C.MINT_LOSS_BAN_1_SEC)
        ban2 = float(C.MINT_LOSS_BAN_2_SEC)
        if losses >= 2 and ban2 > 0:
            until = time.time() + ban2
            label = f"{ban2/3600:.0f}h"
        elif ban1 > 0:
            until = time.time() + ban1
            label = f"{ban1/3600:.0f}h"
        else:
            until = 0.0
            label = "off"
        prev = float(ent.get("ban_until") or 0)
        ent["ban_until"] = max(prev, until)
        self._mint_loss_bans[mint] = ent
        self._persist_mint_loss_bans()
        # 同时拉长普通冷却，且清掉强反转计数资格
        if until > 0:
            self._arm_mint_cooldown(mint, seconds=until - time.time(), reason=f"loss_ban:{reason}")
            self._reentry_used[mint] = max(
                int(self._reentry_used.get(mint) or 0),
                int(C.REENTRY_MAX_RETRY) + 1,
            )
        logger.warning(
            "🔒 mint 亏损封禁 %s… losses_today=%d ban=%s reason=%s",
            mint[:8],
            losses,
            label,
            reason,
        )

    @staticmethod
    def _norm_symbol(sym: str | None) -> str:
        return "".join(ch for ch in (sym or "").upper() if ch.isalnum())

    def _load_symbol_cooldowns(self) -> None:
        try:
            path = C.SYMBOL_COOLDOWN_FILE
            if not path.exists():
                return
            raw = json.loads(path.read_text(encoding="utf-8"))
            now = time.time()
            for sym, until in (raw or {}).items():
                try:
                    u = float(until)
                except (TypeError, ValueError):
                    continue
                if u > now and sym:
                    self._symbol_cooldown_until[str(sym).upper()] = u
            if self._symbol_cooldown_until:
                logger.info(
                    "♻️ 已恢复 %d 个 Symbol 冷却", len(self._symbol_cooldown_until)
                )
        except Exception:
            logger.exception("加载 Symbol 冷却失败（忽略）")

    def _persist_symbol_cooldowns(self) -> None:
        try:
            C.DATA_DIR.mkdir(parents=True, exist_ok=True)
            now = time.time()
            payload = {
                k: v
                for k, v in self._symbol_cooldown_until.items()
                if float(v) > now
            }
            tmp = C.SYMBOL_COOLDOWN_FILE.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(C.SYMBOL_COOLDOWN_FILE)
        except Exception:
            logger.exception("落盘 Symbol 冷却失败")

    def _symbol_cooldown_remaining(self, sym: str | None) -> float:
        key = self._norm_symbol(sym)
        if not key:
            return 0.0
        return max(0.0, float(self._symbol_cooldown_until.get(key) or 0) - time.time())

    def _symbol_permanently_banned(self, sym: str | None) -> bool:
        if not C.SYMBOL_PERMANENT_BAN_ENABLED:
            return False
        key = self._norm_symbol(sym)
        return bool(
            key
            and float(self._symbol_cooldown_until.get(key) or 0)
            >= 253402300799.0
        )

    def entry_block_for(self, mint: str, sym: str | None) -> dict[str, str] | None:
        """看板用：过了硬过滤但仍开不了仓的原因（顺序同 open_long 的闸门）。

        没有它，UI 上的 ✓ 会骗人——永久禁买 / 冷却中的票也显示可开仓。
        """
        if mint in self.positions:
            return {"label": "持仓中", "detail": "已有仓位，不重复开"}
        ban_left = self._mint_loss_ban_remaining(mint)
        if ban_left > 0:
            return {
                "label": "亏损封禁",
                "detail": f"该 mint 亏损硬封禁剩余 {ban_left/3600:.1f}h（不可解）",
            }
        if self._symbol_permanently_banned(sym):
            return {
                "label": "同名永久禁",
                "detail": "该 ticker 已实盘买过，换 mint 也不再买",
            }
        sym_left = self._symbol_cooldown_remaining(sym)
        if sym_left > 0:
            return {
                "label": "同名冷却",
                "detail": f"同 ticker 冷却剩余 {sym_left/60:.0f}m（防换 mint 连环开）",
            }
        cool_left = float(self._mint_cooldown_until.get(mint) or 0) - time.time()
        if cool_left > 0:
            return {
                "label": "熔断冷却",
                "detail": f"该 mint 熔断冷却剩余 {cool_left/60:.0f}m",
            }
        if len(self.positions) >= C.MAX_OPEN_POSITIONS:
            return {
                "label": "仓位已满",
                "detail": f"持仓数已达上限 {C.MAX_OPEN_POSITIONS}",
            }
        return None

    def _arm_symbol_cooldown(
        self,
        sym: str | None,
        *,
        seconds: float | None = None,
        reason: str = "",
        lost: bool = False,
    ) -> None:
        key = self._norm_symbol(sym)
        if not key:
            return
        permanent = bool(C.SYMBOL_PERMANENT_BAN_ENABLED)
        if permanent:
            # 9999-12-31 23:59:59 UTC；JSON 保持向后兼容，无需另建状态文件。
            until = 253402300799.0
            cool = until - time.time()
        elif seconds is None:
            seconds = float(
                C.SYMBOL_LOSS_BAN_SEC if lost else C.SYMBOL_COOLDOWN_SEC
            )
            cool = float(seconds)
            if cool <= 0:
                return
            until = time.time() + cool
        else:
            cool = float(seconds)
            if cool <= 0:
                return
            until = time.time() + cool
        prev = float(self._symbol_cooldown_until.get(key) or 0)
        if until > prev:
            self._symbol_cooldown_until[key] = until
            self._persist_symbol_cooldowns()
            if permanent:
                logger.warning(
                    "🔒 Symbol 永久禁买 %s（换 mint 也拦）reason=%s",
                    key,
                    reason or "bought_once",
                )
            else:
                logger.warning(
                    "🔒 Symbol 冷却 %s %.0fs（至 %.0f）reason=%s lost=%s",
                    key,
                    cool,
                    until,
                    reason or "exit",
                    lost,
                )

    def _seed_symbol_cooldowns_from_trades(self) -> None:
        """重启后按近期同名出场继续冷却（含换 mint 的连环盘）。"""
        try:
            if not C.TRADES_FILE.exists():
                return
            if C.SYMBOL_PERMANENT_BAN_ENABLED:
                # 任一真实买入即永久使用过；不依赖是否有出场记录。
                for line in C.TRADES_FILE.read_text(encoding="utf-8").splitlines():
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    if row.get("dry_run") or row.get("shadow"):
                        continue
                    if row.get("action") != "buy":
                        continue
                    sym = self._norm_symbol(row.get("symbol"))
                    if sym:
                        self._symbol_cooldown_until[sym] = 253402300799.0
                if self._symbol_cooldown_until:
                    self._persist_symbol_cooldowns()
                    logger.info(
                        "♻️ 已从真实买入重建 %d 个 Symbol 永久禁买",
                        len(self._symbol_cooldown_until),
                    )
                return
            cool = max(float(C.SYMBOL_COOLDOWN_SEC), float(C.SYMBOL_LOSS_BAN_SEC))
            if cool <= 0:
                return
            cutoff = time.time() - cool
            exit_actions = {
                "whale_dump",
                "hard_stop",
                "early_fade",
                "time_stop",
                "dead_stop",
                "trail_stop",
                "be_stop",
                "tp1",
                "write_off",
                "liquidity_escape",
                "manual_sell",
                "manual_flatten",
            }
            for line in C.TRADES_FILE.read_text(encoding="utf-8").splitlines()[-400:]:
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get("dry_run") or row.get("shadow"):
                    continue
                if row.get("action") not in exit_actions:
                    continue
                sym = self._norm_symbol(row.get("symbol"))
                if not sym:
                    continue
                ts_raw = row.get("timestamp") or row.get("ts")
                try:
                    if isinstance(ts_raw, (int, float)):
                        ts = float(ts_raw)
                    else:
                        from datetime import datetime

                        ts = datetime.fromisoformat(
                            str(ts_raw).replace("Z", "+00:00")
                        ).timestamp()
                except Exception:
                    continue
                if ts < cutoff:
                    continue
                pnl = row.get("pnl_sol")
                lost = pnl is not None and float(pnl) < 0
                sec = float(C.SYMBOL_LOSS_BAN_SEC if lost else C.SYMBOL_COOLDOWN_SEC)
                until = ts + sec
                prev = float(self._symbol_cooldown_until.get(sym) or 0)
                if until > prev:
                    self._symbol_cooldown_until[sym] = until
            if self._symbol_cooldown_until:
                self._persist_symbol_cooldowns()
                logger.info(
                    "♻️ Symbol 冷却已从成交重建 %d 个",
                    len(self._symbol_cooldown_until),
                )
        except Exception:
            logger.exception("从成交重建 Symbol 冷却失败")

    # ---------- 开发者/部署者画像 ----------
    def _load_creator_stats(self) -> None:
        try:
            path = C.CREATOR_STATS_FILE
            if not path.exists():
                return
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._creator_stats = {
                    str(k): dict(v) for k, v in raw.items() if isinstance(v, dict)
                }
            banned = sum(
                1
                for v in self._creator_stats.values()
                if float(v.get("ban_until") or 0) > time.time()
            )
            if banned:
                logger.info("♻️ 已恢复 %d 个 creator 封禁", banned)
        except Exception:
            logger.exception("加载 creator 画像失败（忽略）")

    def _persist_creator_stats(self) -> None:
        try:
            C.DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp = C.CREATOR_STATS_FILE.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self._creator_stats, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(C.CREATOR_STATS_FILE)
        except Exception:
            logger.exception("落盘 creator 画像失败")

    def _creator_ban_remaining(self, creator: str | None) -> float:
        if not creator:
            return 0.0
        ent = self._creator_stats.get(creator) or {}
        return max(0.0, float(ent.get("ban_until") or 0) - time.time())

    def _record_creator_seen(self, creator: str | None, mint: str) -> int:
        """登记该 creator 名下我们尝试过的 mint（24h 窗口），返回 24h 内不同 mint 数。"""
        if not creator or not mint:
            return 0
        now = time.time()
        ent = dict(self._creator_stats.get(creator) or {})
        mints = dict(ent.get("mints") or {})
        cutoff = now - 86400.0
        mints = {m: t for m, t in mints.items() if float(t) >= cutoff}
        mints[mint] = now
        ent["mints"] = mints
        self._creator_stats[creator] = ent
        self._persist_creator_stats()
        return len(mints)

    def _arm_creator_ban(self, creator: str | None, *, reason: str = "") -> None:
        """creator 名下出现亏损出场 → 全 creator 冷却封禁。"""
        if not creator or float(C.CREATOR_LOSS_BAN_SEC) <= 0:
            return
        now = time.time()
        ent = dict(self._creator_stats.get(creator) or {})
        ent["losses"] = int(ent.get("losses") or 0) + 1
        ent["last_reason"] = reason
        until = now + float(C.CREATOR_LOSS_BAN_SEC)
        ent["ban_until"] = max(float(ent.get("ban_until") or 0), until)
        self._creator_stats[creator] = ent
        self._persist_creator_stats()
        logger.warning(
            "🔒 creator 封禁 %s… losses=%d ban=%.0fh reason=%s",
            creator[:8],
            int(ent["losses"]),
            float(C.CREATOR_LOSS_BAN_SEC) / 3600.0,
            reason,
        )

    def _seed_loss_bans_from_recent_trades(self) -> None:
        """重启后按近期亏损出场重建当日封禁计数。"""
        try:
            if not C.TRADES_FILE.exists():
                return
            day = self._utc_day()
            loss_actions = {
                "whale_dump",
                "hard_stop",
                "early_fade",
                "time_stop",
                "dead_stop",
                "write_off",
                "liquidity_collapse",
                "liquidity_escape",
                "manual_sell",
                "manual_flatten",
            }
            counts: dict[str, int] = {}
            for line in C.TRADES_FILE.read_text(encoding="utf-8").splitlines()[-400:]:
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get("dry_run") or row.get("shadow"):
                    continue
                if row.get("action") not in loss_actions:
                    continue
                mint = row.get("mint")
                if not mint:
                    continue
                pnl = row.get("pnl_sol")
                if pnl is None:
                    continue
                if float(pnl) >= 0:
                    continue
                ts_raw = row.get("timestamp") or row.get("ts")
                try:
                    if isinstance(ts_raw, (int, float)):
                        ts = float(ts_raw)
                    else:
                        ts = datetime.fromisoformat(
                            str(ts_raw).replace("Z", "+00:00")
                        ).timestamp()
                except Exception:
                    continue
                if datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d") != day:
                    continue
                counts[mint] = counts.get(mint, 0) + 1
            now = time.time()
            for mint, n in counts.items():
                ent = dict(self._mint_loss_bans.get(mint) or {})
                prev_n = int(ent.get("losses") or 0) if ent.get("day") == day else 0
                if n <= prev_n and float(ent.get("ban_until") or 0) > now:
                    continue
                ban1 = float(C.MINT_LOSS_BAN_1_SEC)
                ban2 = float(C.MINT_LOSS_BAN_2_SEC)
                until = now + (ban2 if n >= 2 and ban2 > 0 else ban1)
                self._mint_loss_bans[mint] = {
                    "day": day,
                    "losses": max(prev_n, n),
                    "ban_until": max(float(ent.get("ban_until") or 0), until),
                    "last_reason": "seed_replay",
                }
                self._arm_mint_cooldown(
                    mint,
                    seconds=max(0.0, until - now),
                    reason="loss_ban:seed",
                )
                self._reentry_used[mint] = max(
                    int(self._reentry_used.get(mint) or 0),
                    int(C.REENTRY_MAX_RETRY) + 1,
                )
            if counts:
                self._persist_mint_loss_bans()
                logger.info("已按今日日志重建亏损封禁 %d 个 mint", len(counts))
        except Exception:
            logger.exception("重建亏损封禁失败（忽略）")

    # ---------- 持仓持久化（防重启重复开仓 / 旧仓失管）----------
    def _persist_positions(self) -> None:
        try:
            C.DATA_DIR.mkdir(parents=True, exist_ok=True)
            payload = {
                "updated_at": _utc(),
                "dry_run": bool(self.dry_run),
                "shadow": bool(self.shadow),
                "positions": list(self.positions.values()),
            }
            tmp = C.POSITIONS_FILE.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            tmp.replace(C.POSITIONS_FILE)
        except Exception:
            logger.exception("持仓持久化失败")

    def _restore_positions(self) -> None:
        """重启后恢复未平仓仓位；模式不匹配的旧仓直接丢弃。"""
        try:
            if not C.POSITIONS_FILE.exists():
                return
            saved = json.loads(C.POSITIONS_FILE.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("读取持仓文件失败，忽略")
            return
        rows = saved.get("positions") or []
        if not rows:
            return
        # 影子仓位不能带进实盘，反之亦然
        want_shadow = bool(self.shadow)
        want_dry = bool(self.dry_run)
        restored = 0
        for pos in rows:
            if bool(pos.get("shadow")) != want_shadow:
                continue
            if not want_shadow and bool(pos.get("dry_run", True)) != want_dry:
                continue
            mint = pos.get("mint")
            if not mint or float(pos.get("qty_left") or 0) <= 0:
                continue
            self.positions[mint] = pos
            restored += 1
        if restored:
            logger.warning(
                "♻️ 已恢复 %d 个未平仓仓位（重启续管）: %s",
                restored,
                ", ".join(
                    f"{p.get('symbol')}@{float(p.get('entry') or 0):.10g}"
                    for p in self.positions.values()
                ),
            )

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
            # 实盘本金锚点：跨重启保留，避免每次启动把收益率清零
            if getattr(self, "live_bankroll_anchor", None):
                payload["live_bankroll_sol"] = round(float(self.live_bankroll_anchor), 8)
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
        shadow = bool(self.shadow)
        # 影子模式强制虚拟成交：绝不走 Jupiter
        if shadow:
            dry = True
        mint = signal["mint"]
        with self._trade_lock:
            if mint in self.positions or mint in self._opening:
                return None
            ban_left = self._mint_loss_ban_remaining(mint)
            if ban_left > 0:
                logger.info(
                    "开仓跳过 %s：亏损硬封禁剩余 %.0fs（不可解）",
                    signal.get("symbol") or mint[:6],
                    ban_left,
                )
                return None
            sym_left = self._symbol_cooldown_remaining(signal.get("symbol"))
            if sym_left > 0:
                if self._symbol_permanently_banned(signal.get("symbol")):
                    logger.info(
                        "开仓跳过 %s：该 Symbol 已实盘买过，永久禁买（换 mint 也拦）",
                        signal.get("symbol") or mint[:6],
                    )
                else:
                    logger.info(
                        "开仓跳过 %s：同名 Symbol 冷却剩余 %.0fs（防换 mint 连环开）",
                        signal.get("symbol") or mint[:6],
                        sym_left,
                    )
                return None
            cool_until = float(self._mint_cooldown_until.get(mint) or 0)
            if cool_until > time.time() and not self._strong_reversal_unlock(mint, signal):
                logger.info(
                    "开仓跳过 %s：熔断冷却中剩余 %.0fs",
                    signal.get("symbol") or mint[:6],
                    cool_until - time.time(),
                )
                return None
            if len(self.positions) >= C.MAX_OPEN_POSITIONS:
                return None
            self._opening.add(mint)
        try:
            return self._open_long_body(
                signal, dry=dry, shadow=shadow, stop_file=stop_file
            )
        finally:
            with self._trade_lock:
                self._opening.discard(mint)

    def _open_long_body(
        self,
        signal: dict[str, Any],
        *,
        dry: bool,
        shadow: bool,
        stop_file: bool,
    ) -> dict[str, Any] | None:
        mint = signal["mint"]
        # 实盘最后一道去重：钱包已持有该 mint → 说明本地状态丢失（重启等），禁止重复买入
        if not shadow and not dry:
            try:
                from .chain import keypair_for_live
                from .rpc import get_token_balance_raw

                owner = str(keypair_for_live().pubkey())
                held_raw, held_dec = get_token_balance_raw(owner, mint)
                if held_raw > 0:
                    logger.error(
                        "🚨 拒绝重复开仓 %s：钱包已持有 raw=%d（本地无此仓，疑似重启丢状态）",
                        signal.get("symbol") or mint[:6],
                        held_raw,
                    )
                    try:
                        journal.record_alert(
                            action="duplicate_buy_block",
                            message="钱包已持有该代币，拒绝重复买入",
                            mint=mint,
                            symbol=signal.get("symbol") or mint[:6],
                            context={"held_raw": held_raw, "decimals": held_dec},
                        )
                    except Exception:
                        logger.exception("写入重复买入告警失败")
                    return None
            except Exception as exc:
                logger.warning("开仓前链上持仓查重失败（继续）: %s", exc)

        # 绑定池地址（开仓后管仓直接读链上账户，不再走 DexScreener）
        if not signal.get("pool"):
            try:
                from .market_data import lookup_pool

                pool, dex = lookup_pool(mint)
                if pool:
                    signal = {**signal, "pool": pool, "dex": dex or signal.get("dex")}
            except Exception:
                pass

        # 最低分（Found 类 39 分残盘）
        score = float(signal.get("score") or 0)
        if score < float(C.ENTRY_MIN_SCORE):
            logger.warning(
                "开仓跳过 %s：评分 %.1f < %.0f",
                signal.get("symbol") or mint[:6],
                score,
                float(C.ENTRY_MIN_SCORE),
            )
            return None

        # 极早期 bonding curve 禁买（进度 < 阈值）；pumpswap 视为已毕业
        if not shadow and float(C.BONDING_MIN_PROGRESS_PCT) > 0:
            try:
                from .onchain_price import fetch_bonding_progress_pct

                prog, src = fetch_bonding_progress_pct(
                    mint, pool=signal.get("pool"), dex=signal.get("dex")
                )
            except Exception as exc:
                logger.warning("曲线进度查询失败 %s: %s — 按拒绝处理", mint[:8], exc)
                prog, src = None, f"exc:{exc}"
            if prog is None:
                # 认不出场所 ≠ 读取失败：前者是「这个池子我们根本没有判据」，
                # 看板要能一眼分出来，否则又会被当成偶发 RPC 抖动忽略过去。
                unknown_venue = str(src).startswith("unknown_owner")
                logger.warning(
                    "开仓跳过 %s：%s（%s dex=%s）",
                    signal.get("symbol") or mint[:6],
                    "池子程序不在已知场所内，毕业状态未知" if unknown_venue
                    else "无法读取 bonding 进度",
                    src,
                    signal.get("dex"),
                )
                try:
                    journal.record_alert(
                        action=(
                            "unknown_venue_block" if unknown_venue
                            else "bonding_read_fail"
                        ),
                        message=(
                            f"未知交易场所（{src}）— 毕业状态未测，按不通过处理"
                            if unknown_venue
                            else f"bonding 进度读取失败（{src}）"
                        ),
                        mint=mint,
                        symbol=signal.get("symbol") or mint[:6],
                        context={
                            "progress_pct": None,
                            "source": src,
                            "dex": signal.get("dex"),
                            "pool": signal.get("pool"),
                        },
                    )
                except Exception:
                    pass
                return None
            if C.ENTRY_GRADUATED_ONLY and prog < 99.5:
                logger.warning(
                    "开仓跳过 %s：未毕业（bonding %.1f%% < 100%%）— graduated-only 防抽池",
                    signal.get("symbol") or mint[:6],
                    prog,
                )
                try:
                    journal.record_alert(
                        action="graduated_only_block",
                        message=f"未毕业曲线盘 bonding {prog:.1f}%（graduated-only）",
                        mint=mint,
                        symbol=signal.get("symbol") or mint[:6],
                        context={"progress_pct": prog, "source": src},
                    )
                except Exception:
                    pass
                return None
            if prog < float(C.BONDING_MIN_PROGRESS_PCT):
                logger.warning(
                    "开仓跳过 %s：bonding 进度 %.1f%% < %.0f%%（%s）— 极早期土狗",
                    signal.get("symbol") or mint[:6],
                    prog,
                    float(C.BONDING_MIN_PROGRESS_PCT),
                    src,
                )
                try:
                    journal.record_alert(
                        action="bonding_too_early",
                        message=(
                            f"bonding 进度 {prog:.1f}% < {C.BONDING_MIN_PROGRESS_PCT:.0f}%"
                        ),
                        mint=mint,
                        symbol=signal.get("symbol") or mint[:6],
                        context={"progress_pct": prog, "source": src},
                    )
                except Exception:
                    pass
                return None

        # 链上安全审计（防貔貅/增发/撤池）：fail-closed，拿不到数据也拒绝
        creator: str | None = None
        enforce_safety = C.SAFETY_CHECK_ENABLED and (
            (not shadow and not dry) or (shadow and C.SAFETY_ENFORCE_IN_SHADOW)
        )
        if enforce_safety:
            try:
                from . import safety

                verdict = safety.check_token_safety(
                    mint,
                    pool=signal.get("pool"),
                    dex=signal.get("dex"),
                    use_cache=False,  # 下单前强制重审，防扫描→下单竞态窗口内状态变化
                )
            except Exception as exc:
                # 审计模块自身异常也按不通过处理（宁可错过）
                logger.exception("安全审计调用失败，按拒绝处理 %s", mint)
                verdict = None
            if verdict is None or not verdict.ok:
                reasons = verdict.reasons if verdict else ["安全审计调用异常"]
                logger.error(
                    "🚨 链上安全检查未通过（未通过风控白名单）%s: %s",
                    signal.get("symbol") or mint[:6],
                    "; ".join(reasons),
                )
                try:
                    journal.record_alert(
                        action="safety_block",
                        message="链上安全检查未通过：" + "; ".join(reasons),
                        mint=mint,
                        symbol=signal.get("symbol") or mint[:6],
                        context={
                            "reasons": reasons,
                            "checks": verdict.checks if verdict else {},
                            "pool": signal.get("pool"),
                            "dex": signal.get("dex"),
                        },
                    )
                except Exception:
                    logger.exception("写入安全拦截告警失败")
                return None
            # 开仓后早期大户监控：沿用审计时的非流动性大户快照
            whale_snapshot = dict((verdict.checks or {}).get("whale_snapshot") or {})
            creator = (verdict.checks or {}).get("creator")
        else:
            whale_snapshot = {}
            # 即使未强制安全审计，实盘仍尽量拍一张大户快照供早期监控
            if not shadow and not dry:
                try:
                    from . import holders

                    hr = holders.check_holder_concentration(
                        mint, pool=signal.get("pool"), dex=signal.get("dex")
                    )
                    whale_snapshot = dict(hr.whale_snapshot or {})
                except Exception:
                    logger.warning("开仓大户快照失败（继续开仓）%s", mint[:8])

        # 开发者/部署者画像否决：治 USWR 类"换 mint / 换名、同一 creator 连环发盘"
        if C.CREATOR_BAN_ENABLED and creator and not shadow and not dry:
            ban_left = self._creator_ban_remaining(creator)
            if ban_left > 0:
                logger.warning(
                    "开仓跳过 %s：creator %s… 亏损封禁剩余 %.0fs（连环盘）",
                    signal.get("symbol") or mint[:6],
                    creator[:8],
                    ban_left,
                )
                try:
                    journal.record_alert(
                        action="creator_ban",
                        message=f"creator 亏损封禁剩余 {ban_left:.0f}s",
                        mint=mint,
                        symbol=signal.get("symbol") or mint[:6],
                        context={"creator": creator},
                    )
                except Exception:
                    pass
                return None
            seen = self._record_creator_seen(creator, mint)
            if C.CREATOR_MAX_DEPLOYS_24H > 0 and seen >= int(C.CREATOR_MAX_DEPLOYS_24H):
                logger.warning(
                    "开仓跳过 %s：creator %s… 24h 内第 %d 个不同 mint ≥ %d（连环发盘）",
                    signal.get("symbol") or mint[:6],
                    creator[:8],
                    seen,
                    int(C.CREATOR_MAX_DEPLOYS_24H),
                )
                try:
                    journal.record_alert(
                        action="creator_serial_deploy",
                        message=f"creator 24h 内 {seen} 个 mint ≥ {C.CREATOR_MAX_DEPLOYS_24H}",
                        mint=mint,
                        symbol=signal.get("symbol") or mint[:6],
                        context={"creator": creator, "mints_24h": seen},
                    )
                except Exception:
                    pass
                return None

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
                    # 看板严重滞后于拉升中的链上价 → 直接放弃，别用更贵的链上价硬追
                    if drift >= float(C.ENTRY_BOARD_CHAIN_DRIFT_MAX):
                        logger.warning(
                            "开仓放弃 %s：看板→链上偏离 %+0.1f%% ≥ +%.0f%% "
                            "(board=%.8g chain=%.8g) — 疑似追高",
                            signal.get("symbol") or mint[:6],
                            drift * 100,
                            float(C.ENTRY_BOARD_CHAIN_DRIFT_MAX) * 100,
                            mid,
                            chain_px,
                        )
                        return None
                    # 链上价是开仓参考价的首选：它是我们实际成交的那条曲线。
                    #
                    # 曾经这里只在链上价「更高」时才采信，那是 PumpSwap 虚拟储备
                    # 被漏读时期的临时规避 —— 当时链上价恒被低报 1+17.5845/池内SOL
                    # 倍，无条件采信会把参考价换成假低价，Jupiter 的正确报价相对它
                    # 凭空贵 16~24%，每笔都被往返闸拦掉。虚拟储备补上后偏差已收敛到
                    # 1% 量级且不随池深变化，那个非对称规避反而有害：链上价真的下跌
                    # 时不更新 mid，会拿偏高的陈旧看板价当参考，上面那道追高闸也跟着
                    # 失准。故恢复对称采信。
                    if abs(drift) >= 0.02:
                        logger.warning(
                            "开仓改用链上价 %s board=%.8g chain=%.8g "
                            "drift=%+.1f%% src=%s",
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

        # 买前短时确认：信号过线后再看几秒，仍在阴跌就不接刀（实盘生效）
        if not shadow and not dry:
            ok_entry, mid_confirmed = self._confirm_entry_price(signal, mid)
            if not ok_entry:
                return None
            if mid_confirmed > 0:
                mid = mid_confirmed

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
            # Micro-Live：固定小额 LIVE_SIZE_SOL（clamp 在 risk 层做）
            want_sol = (
                float(C.LIVE_SIZE_SOL) if C.MICRO_LIVE else self.equity() * C.POSITION_PCT
            )
            try:
                gate = risk_guard.pre_trade_gate(
                    side="buy",
                    equity=self.equity(),
                    cash=self.cash,
                    amount_sol=want_sol,
                    slippage_bps=C.ENTRY_MAX_SLIPPAGE_BPS,
                    stop_file=stop_file,
                )
            except RiskBlocked as exc:
                logger.error("开仓被风控拦截: %s", exc)
                return None

            sol = float(gate["amount_sol"])
            slip_bps = int(gate["slippage_bps"])
            confirm_ref = mid  # 确认后链上价；成交后 mid 会被改写成 fill

            if not dry:
                # LIVE：钱包 + Jupiter 实盘换币
                from .live_swap import LiveSwapError, buy_token_with_sol

                try:
                    from .chain import keypair_for_live

                    _kp = keypair_for_live()
                    logger.info(
                        "LIVE open 钱包 %s…%s rpc 入场滑点=%dbps(%.1f%%) 确认价=%.10g",
                        str(_kp.pubkey())[:4],
                        str(_kp.pubkey())[-4:],
                        slip_bps,
                        slip_bps / 100.0,
                        confirm_ref,
                    )
                    live_meta = buy_token_with_sol(
                        token_mint=mint,
                        sol_amount=sol,
                        slippage_bps=slip_bps,
                        equity=self.equity(),
                        cash=self.cash,
                        stop_file=stop_file,
                        ref_price_sol=confirm_ref,
                        pool=signal.get("pool"),
                        dex=signal.get("dex"),
                    )
                    sol = float(live_meta.get("sol_amount") or sol)
                    if live_meta.get("qty"):
                        qty = float(live_meta["qty"])
                    else:
                        qty = sol / confirm_ref
                    if live_meta.get("fill_price"):
                        mid = float(live_meta["fill_price"])
                    # 相对确认价超偏离，或相对报价超入场滑点硬顶 → mint 冷却
                    fill_px_live = float(live_meta.get("fill_price") or mid)
                    fill_vs_confirm = None
                    if confirm_ref > 0 and fill_px_live > 0:
                        fill_vs_confirm = (fill_px_live - confirm_ref) / confirm_ref
                    slip_real = live_meta.get("slippage_real_pct")
                    max_slip_pct = float(slip_bps) / 100.0
                    # confirm_ref 可能来自 gecko/看板，与成交口径有基差（实测中位 ~5%），
                    # 故这里用 fallback 门槛；同源的严格判定在报价闸里对现读链上价做。
                    max_gap = float(C.ENTRY_QUOTE_GAP_MAX_FALLBACK)
                    overshoot = (
                        (
                            slip_real is not None
                            and float(slip_real) > max_slip_pct
                        )
                        or (
                            fill_vs_confirm is not None
                            and fill_vs_confirm > max_gap
                        )
                    )
                    if overshoot and float(C.ENTRY_SLIP_OVERSHOOT_COOLDOWN_SEC) > 0:
                        logger.warning(
                            "买入偏离过大 fill_vs_confirm=%s slip_real=%s "
                            "(gap硬顶 %.1f%% / 滑点硬顶 %.1f%%) — mint 冷却 %.0fs",
                            (
                                f"{fill_vs_confirm*100:+.2f}%"
                                if fill_vs_confirm is not None
                                else "?"
                            ),
                            (
                                f"{float(slip_real):+.2f}%"
                                if slip_real is not None
                                else "?"
                            ),
                            max_gap * 100,
                            max_slip_pct,
                            float(C.ENTRY_SLIP_OVERSHOOT_COOLDOWN_SEC),
                        )
                        self._arm_mint_cooldown(
                            mint,
                            seconds=float(C.ENTRY_SLIP_OVERSHOOT_COOLDOWN_SEC),
                            reason="slip_overshoot",
                            entry_ref=mid,
                        )
                except (RiskBlocked, LiveSwapError, Exception) as exc:
                    logger.error("LIVE 开仓中止：%s", exc)
                    return None
            else:
                preview = AL.pump_trade_costs(amount_sol=sol, side="buy")
                if sol + preview["total_friction_sol"] > self.cash:
                    return None
                qty = sol / mid

        # ---- 标价基准（口径一致性）----
        # entry 记的是 Jupiter 真实成交价（含买方滑点+手续费），而之后每轮
        # 管仓用的是链上池价。两者不同源：任何常数基差都会被 (px-entry)/entry
        # 直接当成浮盈亏，把整条出场阶梯（TP1/移动止盈/硬止损）整体平移。
        # 历史事故：链上池价漏算虚拟储备低报 1.05~1.5 倍 → 开仓瞬间就显示
        # −5%~−33% 假浮亏，硬止损线 −13% 变成「真实盈亏跌破 −3%~+9% 就砍」，
        # 且 TP1 永远够不着（12 笔受影响仓位的 max_float_pnl_pct 全是 0.00）。
        # 修法：成交后立刻按**标价口径**再读一次现价存为 entry_mark，管仓的
        # 盈亏一律 mark 对 mark 算；成交价只用于账本结算（_close_partial）。
        # 开仓前读不到链上价的（曲线外池 / RPC 不可用 / demo），之后也标不了价，
        # 没必要多打一次 RPC，直接退回成交价基准。
        entry_mark = 0.0
        if onchain_meta and float(onchain_meta.get("price") or 0) > 0:
            entry_mark = self._read_entry_mark(mint, signal, fill_ref=mid)

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
            # peak 只服务移动止盈线（peak×(1−trail)）与 dead_stop 的 peak_pnl，
            # 两者都拿它跟 mark 口径的现价/基准比，所以种子必须是 entry_mark 而不是
            # 成交价。成交价含买方滑点+DEX 费，结构性高于成交后的池价（实测 CXMT
            # 成交价比报价 +4.11%，比成交后池价 +5.2%），拿它当峰值等于开仓瞬间就
            # 白送一个虚假峰值：移动止盈线整体上移，回撤止盈提前开火；dead_stop 的
            # peak_pnl 起点变成 +2%~+6%，一旦超过 DEAD_CUT_MIN_PNL 就永远砍不动。
            "peak": entry_mark or mid,
            # 峰值算在哪个基准上（同 _pos_metrics 的 float_basis）：缺了它，复盘时
            # 分不清 trail_line 是 mark 口径还是成交价口径。
            "peak_basis": "entry_mark" if entry_mark else "fill",
            "tp1_done": False,
            "trail_line": None,
            "dry_run": dry,
            "shadow": shadow,
            "status": "open",
            "pool": signal.get("pool"),
            "dex": signal.get("dex"),
            "creator": creator,
            # 管仓盈亏的基准（与后续 mark 同源）；读不到时退回成交价
            "entry_mark": entry_mark or None,
            "price_source": (onchain_meta or {}).get("source") or "signal",
            "entry_sol_vault": float((onchain_meta or {}).get("sol_vault") or 0) or None,
            "sol_vault": float((onchain_meta or {}).get("sol_vault") or 0) or None,
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
            "track": signal.get("track") or "A",
            "slippage_pct": shadow_slip_pct if shadow else costs["slippage_pct"],
            "slippage_bps": int(C.SHADOW_SLIPPAGE_BPS) if shadow else slip_bps,
            "fees_sol": 0.0,
            "gas_sol": 0.0,
            "slippage_sol": 0.0,
            "fill_entry": fill_px,
            "tx_signature": None if shadow else live_meta.get("signature"),
            # None = 还没标过价。绝不能初始化成 0.0：那会变成极值的下限/上限，
            # 把「没测到」伪装成「测到了 0.00%」。
            "max_float_pnl_pct": None,
            "max_float_pnl_sol": None,
            "min_float_pnl_pct": None,
            "min_float_pnl_sol": None,
            "realized_pnl_sol": 0.0,
            # 开仓时非流动性大户快照（早期砸盘熔断用）
            "whale_snapshot": whale_snapshot,
            "whale_dump_done": False,
            "whale_last_poll": 0.0,
            # 静默期结束后按成交后持仓重拍基线，再开始判定；连续确认计数
            "whale_baseline_ready": False,
            "whale_strikes": 0,
        }
        self.cash -= sol
        self._last_fill_mono = time.monotonic()
        with self._trade_lock:
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
            # 实盘：滑点已含在链上成交价里；gas 记真实消耗（回读失败回落名义值）
            real_gas = float(live_meta.get("gas_sol") or 0.000005)
            pos["gas_sol"] = float(pos.get("gas_sol") or 0) + real_gas
            self.total_gas += real_gas
            self.cash -= real_gas
            pos["quote_price"] = live_meta.get("quote_price")
            pos["slippage_real_pct"] = live_meta.get("slippage_real_pct")
            pump_ledger.append({
                "kind": "gas",
                "amount": real_gas,
                "symbol": pos.get("symbol"),
                "position_id": pos.get("id"),
                "note": "live_buy",
                "meta": {
                    "signature": live_meta.get("signature"),
                    "quote_price": live_meta.get("quote_price"),
                    "fill_price": live_meta.get("fill_price"),
                    "slippage_real_pct": live_meta.get("slippage_real_pct"),
                },
            })
        self._persist_account()
        self._persist_positions()
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
            scoring=signal.get("scoring"),
            entry_gate=_entry_gate_snapshot(pos.get("track")),
        )
        trade["fee_sol"] = pos["fees_sol"]
        trade["gas_sol"] = pos["gas_sol"]
        trade["slippage_sol"] = pos["slippage_sol"]
        trade["fill_price"] = fill_px
        trade["tx_signature"] = None if shadow else live_meta.get("signature")
        if not shadow and not dry:
            trade["quote_price"] = live_meta.get("quote_price")
            trade["slippage_real_pct"] = live_meta.get("slippage_real_pct")
            # 买入成功即永久占用 ticker；进程崩溃前也立即落盘。
            self._arm_symbol_cooldown(
                pos.get("symbol"), reason="bought_once", lost=False
            )
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
            trail = _exit_params(pos)["trail"]
            pos["trail_line"] = float(pos["peak"]) * (1.0 - trail)
        basis = mark_basis(pos)
        pos["pnl_pct"] = (price - basis) / basis if basis else 0.0
        # 账本口径浮盈亏（成交价对链上价）：只做展示/审计，不驱动出场
        entry = float(pos["entry"])
        pos["pnl_pct_vs_fill"] = (price - entry) / entry if entry else 0.0
        # 浮盈亏极值（mark 口径，与出场阶梯同源）。
        # 旧写法把 max 初始化成 0.0 再只许上调，等于给它加了个 0 下限：全程水下的
        # 仓位永远记成「峰值 +0.00%」——一个从未出现过的读数，却长得像测量值。
        # 只记最高点也看不出「先跌 30% 再回本」和「一路阴跌」的区别，所以同时记最低点。
        float_pct = float(pos["pnl_pct"]) * 100.0
        float_sol = float(pos.get("qty_left") or 0) * (price - basis)
        prev_max = pos.get("max_float_pnl_pct")
        if prev_max is None or float_pct > float(prev_max):
            pos["max_float_pnl_pct"] = round(float_pct, 4)
            pos["max_float_pnl_sol"] = round(float_sol, 8)
        prev_min = pos.get("min_float_pnl_pct")
        if prev_min is None or float_pct < float(prev_min):
            pos["min_float_pnl_pct"] = round(float_pct, 4)
            pos["min_float_pnl_sol"] = round(float_sol, 8)
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
        # 仅看仓位/经纪商标记，避免测试或环境里的全局 SHADOW_MODE 污染影子日志
        shadow = bool(pos.get("shadow") or self.shadow)
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
            from .live_swap import LiquidityCollapse, LiveSwapError, sell_token_for_sol

            # 保命单（止损类）：允许滑点逐级升级重试，绝不卡在 Mempool
            urgent = reason in (
                "hard_stop",
                "early_fade",
                "time_stop",
                "dead_stop",
                "be_stop",
                "trail_stop",
                "whale_dump",
                "manual_flatten",
                "liquidity_escape",
                "stale_mark",
            )
            # 抽池卡住超过阈值 → 强制 urgent salvage
            illiquid_since = float(pos.get("illiquid_since") or 0)
            if (
                illiquid_since > 0
                and (time.time() - illiquid_since) >= float(C.ILLIQUID_FORCE_SELL_SEC)
            ):
                urgent = True
                logger.warning(
                    "⏳ %s illiquid %.0fs ≥ %.0fs — 强制 salvage 卖出",
                    pos["symbol"],
                    time.time() - illiquid_since,
                    float(C.ILLIQUID_FORCE_SELL_SEC),
                )
            try:
                decimals = int(pos.get("decimals") or 6)
                qty_raw_total = int(pos.get("qty_raw") or round(float(pos["qty"]) * (10 ** decimals)))

                # 卖出前以链上真实余额为准（防止本地纸面数量与钱包脱节）
                try:
                    from .chain import keypair_for_live
                    from .rpc import get_token_balance_raw

                    owner = str(keypair_for_live().pubkey())
                    chain_raw, chain_dec = get_token_balance_raw(owner, pos["mint"])
                    if chain_dec:
                        decimals = chain_dec
                    if chain_raw >= 0 and abs(chain_raw - qty_raw_total) > max(1, qty_raw_total // 1000):
                        logger.warning(
                            "⚖️ 持仓对账 %s 本地raw=%d 链上raw=%d → 以链上为准",
                            pos["symbol"], qty_raw_total, chain_raw,
                        )
                        qty_raw_total = chain_raw
                        pos["qty_raw"] = chain_raw
                        pos["decimals"] = decimals
                    if qty_raw_total <= 0:
                        logger.error("链上余额为 0，仓位视为已清 %s", pos["symbol"])
                        pos["qty_left"] = 0.0
                        return {}
                except Exception as exc:
                    logger.warning("卖出前链上余额对账失败（用本地值继续）: %s", exc)

                raw_sell = max(1, int(round(qty_raw_total * ratio)))
                if ratio >= 0.999:
                    raw_sell = qty_raw_total  # 全平：卖光链上真实余额，不留尘埃
                # 兑现预期：盘口估值与「成本×地板」取大，避免盘口已崩时跳过坍塌校验
                cost_floor = qty * entry * float(C.EXIT_EXPECT_COST_FLOOR)
                expect_sol = max(qty * mid, cost_floor)
                live_meta = sell_token_for_sol(
                    token_mint=pos["mint"],
                    token_amount_raw=raw_sell,
                    decimals=decimals,
                    slippage_bps=slip_bps,
                    equity=self.equity(),
                    approx_sol=expect_sol,
                    urgent=urgent,
                )
                proceeds = float(live_meta.get("sol_amount") or (qty * mid))
                if live_meta.get("fill_price"):
                    mid = float(live_meta["fill_price"])
                # 同步剩余 raw
                pos["qty_raw"] = max(0, qty_raw_total - raw_sell)
                pos.pop("illiquid_since", None)
                pos.pop("illiquid_note", None)
            except LiquidityCollapse as exc:
                # 盘口价不可兑现（抽池/假价）：先记 illiquid；若已超时则上层会再以 urgent 重试
                pos["illiquid_since"] = pos.get("illiquid_since") or time.time()
                pos["illiquid_note"] = str(exc)
                logger.error(
                    "🚨 放弃卖出 %s reason=%s：%s（仓位保留，等流动性恢复/强制 salvage）",
                    pos["symbol"], reason, exc,
                )
                journal.record_alert(
                    action="liquidity_collapse",
                    message=f"{pos['symbol']} {reason} 放弃卖出：{exc}",
                    mint=pos["mint"],
                    symbol=pos["symbol"],
                    context={"reason": reason, "ratio": ratio, "mark": mid},
                )
                return {}
            except (RiskBlocked, LiveSwapError, Exception) as exc:
                logger.error(
                    "🚨 LIVE 平仓失败（保留仓位，下轮重试）%s reason=%s urgent=%s: %s",
                    pos["symbol"], reason, urgent, exc,
                )
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
            real_gas = float(live_meta.get("gas_sol") or 0.000005)
            costs = {"fee_sol": 0.0, "gas_sol": real_gas, "slippage_sol": 0.0, "slippage_pct": slip_bps / 10_000.0}
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
                "meta": {
                    "signature": live_meta.get("signature"),
                    "quote_price": live_meta.get("quote_price"),
                    "fill_price": live_meta.get("fill_price"),
                    "slippage_real_pct": live_meta.get("slippage_real_pct"),
                },
            })

        self.cash += proceeds
        self._last_fill_mono = time.monotonic()
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
        self._persist_positions()
        net = gross - costs["fee_sol"] - costs["gas_sol"] - costs["slippage_sol"]
        pos["realized_pnl_sol"] = float(pos.get("realized_pnl_sol") or 0) + net
        logger.info(
            "SETTLE %s %s gross=%+.6f fee=%.6f gas=%.6f slip=%.6f net=%+.6f equity=%.6f sig=%s",
            reason, pos["symbol"], gross, costs["fee_sol"], costs["gas_sol"],
            costs["slippage_sol"], net, self.equity(),
            "virtual" if shadow else (live_meta.get("signature") or "—")[:12],
        )
        fired_threshold, fired_sell = _fired_threshold(pos, reason)
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
            threshold_pct=fired_threshold,
            sell_ratio=fired_sell,
            basis_price=mark_basis(pos) or None,
        )
        trade["gross_pnl_sol"] = round(gross, 8)
        trade["fee_sol"] = costs["fee_sol"]
        trade["gas_sol"] = costs["gas_sol"]
        trade["slippage_sol"] = costs["slippage_sol"]
        trade["fill_price"] = fill_px
        trade["tx_signature"] = None if shadow else live_meta.get("signature")
        if not shadow and not dry:
            trade["quote_price"] = live_meta.get("quote_price")
            trade["slippage_real_pct"] = live_meta.get("slippage_real_pct")
        trade["max_float_pnl_pct"] = pos.get("max_float_pnl_pct")
        # 同名 Symbol 冷却：换 mint 也拦（USWR 今天 4 个合约）
        if not shadow and not dry:
            self._arm_symbol_cooldown(
                pos.get("symbol"),
                reason=reason,
                lost=net < 0,
            )
            # 亏损出场 → 封禁该 creator，换 mint/换名也拦（连环盘同一部署者）
            if net < 0 and C.CREATOR_BAN_ENABLED:
                self._arm_creator_ban(pos.get("creator"), reason=reason)
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
        """出场管理。判定顺序即优先级，前面的分支 continue 掉后面就不再看：

        ⓪  抽池卡住超时 salvage      ILLIQUID_FORCE_SELL_SEC
        ⓪-b 金库骤降 salvage          VAULT_DRAIN_DROP_PCT
        ⓪-c 标价冻结超时 salvage      MARK_STALE_MAX_SEC
        ①  崩塌止损 / 硬止损          PANIC_STOP_PCT / TRACK_x_HARD_STOP
                                      （硬止损需 HARD_STOP_CONFIRM_TICKS/SEC 连续确认）
        ①.2 早期闷亏早砍              EARLY_FADE_*
        ①.25 早期大户净流出熔断        EARLY_WHALE_*
        ①.5 死盘早砍                  DEAD_CUT_*
        ②  时间止损：已停用（TRACK_x_TIME_STOP 仍在配置里但此处不再读）
        ③  TP1                        TRACK_x_TP1 / TRACK_x_TP1_SELL
        ④  移动止盈 / 保本止损        TRACK_x_TRAIL（保本分支见下方注释，当前不可达）

        阈值一律不写死在这段文档里：同一份历史里 hard_stop 出现过 −13%/−22%/−35%，
        写死的数字会在复盘时冒充「当时生效的规则」。要看实际值请读配置或
        _exit_params()。
        """
        events: list[dict[str, Any]] = []
        now = time.time()
        for mint, pos in list(self.positions.items()):
            px = price_map.get(mint)
            if px is None:
                px = float(pos.get("mark") or pos["entry"])
            self.mark(mint, px)
            age_m = (now - float(pos["opened_at"])) / 60.0
            age_s = age_m * 60.0
            entry = float(pos["entry"])
            # 出场判定一律用 mark 口径的基准，避免成交价↔链上价的基差
            # 被当成浮盈亏（见 mark_basis / _read_entry_mark 注释）
            basis = mark_basis(pos) or entry
            pnl_pct = (px - basis) / basis if basis else 0.0
            peak = float(pos.get("peak") or basis)
            peak_pnl = (peak - basis) / basis if basis else 0.0
            xp = _exit_params(pos)

            # ⓪ 抽池卡住超时 → 强制 salvage（优先于一切止盈逻辑）
            illiquid_since = float(pos.get("illiquid_since") or 0)
            if (
                illiquid_since > 0
                and (now - illiquid_since) >= float(C.ILLIQUID_FORCE_SELL_SEC)
                and not pos.get("shadow")
                and not pos.get("dry_run")
            ):
                trade = self._close_partial(pos, 1.0, px, "liquidity_escape")
                if trade:
                    self._arm_mint_cooldown(
                        mint, reason="liquidity_escape", entry_ref=entry
                    )
                    self._record_mint_loss(
                        mint,
                        reason="liquidity_escape",
                        pnl_sol=(trade or {}).get("pnl_sol"),
                    )
                    events.append(
                        {
                            "type": "liquidity_escape",
                            "symbol": pos["symbol"],
                            "mint": mint,
                            "price": px,
                            "pnl_pct": pnl_pct,
                            "illiquid_sec": now - illiquid_since,
                            "trade": trade,
                        }
                    )
                    logger.error(
                        "🚨 LIQUIDITY_ESCAPE %s illiquid=%.0fs — 强制 salvage",
                        pos["symbol"],
                        now - illiquid_since,
                    )
                    self.positions.pop(mint, None)
                    continue

            # ⓪-b 金库 SOL 骤降（砸盘抽干）→ 立刻 salvage，不等假 mark / 硬止损确认
            # CXMT：+23% 时 vault 被砸干，旧逻辑读价失败继续沿用过期 mark，最终 -99.9%
            if (
                pos.get("vault_drain")
                and not pos.get("shadow")
                and not pos.get("dry_run")
            ):
                trade = self._close_partial(pos, 1.0, px, "liquidity_escape")
                if trade:
                    self._arm_mint_cooldown(
                        mint, reason="liquidity_escape", entry_ref=entry
                    )
                    self._record_mint_loss(
                        mint,
                        reason="liquidity_escape",
                        pnl_sol=(trade or {}).get("pnl_sol"),
                    )
                    events.append(
                        {
                            "type": "vault_drain_escape",
                            "symbol": pos["symbol"],
                            "mint": mint,
                            "price": px,
                            "pnl_pct": pnl_pct,
                            "vault_drop": pos.get("vault_drain_drop"),
                            "sol_vault": pos.get("sol_vault"),
                            "entry_sol_vault": pos.get("entry_sol_vault"),
                            "trade": trade,
                        }
                    )
                    logger.error(
                        "🚨 VAULT_DRAIN_ESCAPE %s drop=%.0f%% vault=%.3f→%.3f SOL — 强制 salvage",
                        pos["symbol"],
                        float(pos.get("vault_drain_drop") or 0) * 100,
                        float(pos.get("entry_sol_vault") or 0),
                        float(pos.get("sol_vault") or 0),
                    )
                    self.positions.pop(mint, None)
                    continue

            # ⓪-c 链上价读不到超时 → 强制 salvage。
            # 与 ⓪-b 的区别：那里是「读到了，池子确实空了」，这里是「根本读不到」，
            # 两者绝不能混。读不到时 px 还是上一轮的死数，下面所有阶梯判定都是
            # 拿它跟自己比，永远不会触发——所以必须在这里截断。
            stale_since = float(pos.get("mark_stale_since") or 0)
            stale_limit = float(C.MARK_STALE_MAX_SEC)
            stale_sec = (now - stale_since) if stale_since > 0 else 0.0
            if (
                stale_limit > 0
                and stale_since > 0
                and stale_sec >= stale_limit
                and not pos.get("shadow")
                and not pos.get("dry_run")
            ):
                trade = self._close_partial(pos, 1.0, px, "stale_mark")
                if trade:
                    self._arm_mint_cooldown(mint, reason="stale_mark", entry_ref=entry)
                    self._record_mint_loss(
                        mint,
                        reason="stale_mark",
                        pnl_sol=(trade or {}).get("pnl_sol"),
                    )
                    events.append(
                        {
                            "type": "stale_mark_escape",
                            "symbol": pos["symbol"],
                            "mint": mint,
                            "price": px,
                            "pnl_pct": pnl_pct,
                            "stale_sec": round(stale_sec, 1),
                            "stale_reason": pos.get("mark_stale_reason"),
                            "dex": pos.get("dex"),
                            "trade": trade,
                        }
                    )
                    logger.error(
                        "🚨 STALE_MARK_ESCAPE %s 链上价已 %.0fs 读不到（%s dex=%s）"
                        "— mark 停在 %.10g，强制 salvage",
                        pos["symbol"],
                        stale_sec,
                        pos.get("mark_stale_reason"),
                        pos.get("dex"),
                        px,
                    )
                    try:
                        journal.record_alert(
                            action="stale_mark",
                            message=(
                                f"{pos['symbol']} 链上价 {stale_sec:.0f}s 读不到"
                                f"（{pos.get('mark_stale_reason')}）— 强制离场"
                            ),
                            mint=mint,
                            symbol=pos["symbol"],
                            context={
                                "stale_sec": round(stale_sec, 1),
                                "stale_reason": pos.get("mark_stale_reason"),
                                "dex": pos.get("dex"),
                                "pool": pos.get("pool"),
                                "frozen_mark": px,
                            },
                        )
                    except Exception:
                        logger.exception("写入过期标价告警失败")
                    self.positions.pop(mint, None)
                    continue

            # ① 价格硬止损（最高优先级）：崩塌立即逃生，否则需连续确认
            hard_stop = float(xp["hard_stop"])
            panic_stop = max(hard_stop, float(C.PANIC_STOP_PCT))
            fire_stop = False
            if pnl_pct <= -panic_stop:
                fire_stop = True
                pos["stop_fired_threshold"] = panic_stop
                logger.error(
                    "🚨 PANIC_STOP[%s] %s @%.8g (%.1f%%) ≤ -%.0f%% — 不等确认，立即清仓",
                    pos.get("track") or "A",
                    pos["symbol"],
                    px,
                    pnl_pct * 100,
                    panic_stop * 100,
                )
            elif pnl_pct <= -hard_stop:
                armed_ts = float(pos.get("stop_arm_ts") or 0)
                if armed_ts <= 0:
                    pos["stop_arm_ts"] = now
                    pos["stop_arm_ticks"] = 1
                    logger.warning(
                        "⚠️ 止损警戒 %s @%.8g (%.1f%%) — 需连续 %d 次且 %.0fs 仍破线才砍",
                        pos["symbol"],
                        px,
                        pnl_pct * 100,
                        int(C.HARD_STOP_CONFIRM_TICKS),
                        float(C.HARD_STOP_CONFIRM_SEC),
                    )
                else:
                    ticks = int(pos.get("stop_arm_ticks") or 0) + 1
                    pos["stop_arm_ticks"] = ticks
                    held = now - armed_ts
                    if (
                        ticks >= int(C.HARD_STOP_CONFIRM_TICKS)
                        and held >= float(C.HARD_STOP_CONFIRM_SEC)
                    ):
                        fire_stop = True
                        pos["stop_fired_threshold"] = hard_stop
                        logger.error(
                            "🚨 HARD_STOP[%s] %s @%.8g (%.1f%%) age=%.1fm "
                            "确认 %d 次/%.0fs — 全仓斩仓",
                            pos.get("track") or "A",
                            pos["symbol"],
                            px,
                            pnl_pct * 100,
                            age_m,
                            ticks,
                            held,
                        )
            elif pos.get("stop_arm_ts"):
                pos.pop("stop_arm_ts", None)
                pos.pop("stop_arm_ticks", None)
                logger.info(
                    "止损警戒解除 %s @%.8g (%.1f%%) — 已收回止损线上方",
                    pos["symbol"],
                    px,
                    pnl_pct * 100,
                )

            if fire_stop:
                trade = self._close_partial(pos, 1.0, px, "hard_stop")
                if not trade:
                    # 卖出失败：仓位必须保留，下轮重试。绝不能账本先删、链上还留币。
                    continue
                events.append(
                    {
                        "type": "hard_stop",
                        "symbol": pos["symbol"],
                        "mint": mint,
                        "price": px,
                        "pnl_pct": pnl_pct,
                        "age_m": age_m,
                        "track": pos.get("track"),
                        "trade": trade,
                    }
                )
                self._arm_mint_cooldown(mint, reason="hard_stop", entry_ref=entry)
                self._record_mint_loss(
                    mint,
                    reason="hard_stop",
                    pnl_sol=(trade or {}).get("pnl_sol"),
                )
                self.positions.pop(mint, None)
                continue

            # ①.2 早期闷亏早砍：从未真正浮盈 + 已明显变红 → 先砍小亏，不等 -22%
            # 今日多数磨损单 maxFloat=0，硬止损才砍到 -15%~-30%，是盈亏比崩掉的主因之一
            if (
                C.EARLY_FADE_ENABLED
                and not pos.get("tp1_done")
                and not pos.get("be_takeover")
                and not pos.get("shadow")
                and not pos.get("dry_run")
                and age_s >= float(C.EARLY_FADE_SEC)
                and float(pos.get("max_float_pnl_pct") or 0) / 100.0
                <= float(C.EARLY_FADE_MAX_PEAK)
                and pnl_pct <= -float(C.EARLY_FADE_MIN_LOSS)
            ):
                trade = self._close_partial(pos, 1.0, px, "early_fade")
                if not trade:
                    continue
                events.append(
                    {
                        "type": "early_fade",
                        "symbol": pos["symbol"],
                        "mint": mint,
                        "price": px,
                        "pnl_pct": pnl_pct,
                        "max_float_pnl_pct": pos.get("max_float_pnl_pct"),
                        "age_s": age_s,
                        "trade": trade,
                    }
                )
                logger.warning(
                    "🧹 EARLY_FADE %s age=%.0fs pnl=%.1f%% maxFloat=%.1f%% — 闷亏早砍",
                    pos["symbol"],
                    age_s,
                    pnl_pct * 100,
                    float(pos.get("max_float_pnl_pct") or 0),
                )
                self._arm_mint_cooldown(mint, reason="early_fade", entry_ref=entry)
                self._record_mint_loss(
                    mint,
                    reason="early_fade",
                    pnl_sol=(trade or {}).get("pnl_sol"),
                )
                self.positions.pop(mint, None)
                continue

            # ①.25 早期大户/老鼠仓净流出熔断（开仓后 1~2 分钟）
            # 不等到硬止损 -13%：大户持续抛售 → 立刻全仓离场
            if (
                C.EARLY_WHALE_CHECK_ENABLED
                and not pos.get("whale_dump_done")
                and not pos.get("shadow")
                and not pos.get("dry_run")
                and age_s >= float(C.EARLY_WHALE_GRACE_SEC)
                and age_s <= float(C.EARLY_WHALE_WINDOW_SEC)
                and pos.get("whale_snapshot")
                and (now - float(pos.get("whale_last_poll") or 0))
                >= float(C.EARLY_WHALE_POLL_SEC)
            ):
                pos["whale_last_poll"] = now
                try:
                    from . import holders

                    # 静默期结束后先「重拍基线」：以成交后的真实持仓分布为准，
                    # 不再拿开仓前的快照对拍（否则我们自己的买单和成交churn都算成流出）
                    if not pos.get("whale_baseline_ready"):
                        try:
                            hr = holders.check_holder_concentration(
                                mint, pool=pos.get("pool"), dex=pos.get("dex"),
                                use_cache=False,
                            )
                            fresh = dict(hr.whale_snapshot or {})
                            if fresh:
                                pos["whale_snapshot"] = fresh
                                pos["whale_baseline_ready"] = True
                                logger.info(
                                    "大户基线已按成交后持仓重拍 %s age=%.0fs tracked=%d",
                                    pos["symbol"],
                                    age_s,
                                    len(fresh),
                                )
                                self._persist_positions()
                        except Exception:
                            logger.warning(
                                "大户基线重拍失败 %s（下轮再试）", pos.get("symbol")
                            )
                        # 本轮只建基线，不做判定
                        continue

                    dump, dump_meta = holders.detect_early_whale_dump(
                        mint,
                        snapshot=pos.get("whale_snapshot") or {},
                        pool=pos.get("pool"),
                    )
                    # 价格未明显下跌 → 不认砸盘（防误报砍飞真涨）
                    if dump and pnl_pct > float(C.EARLY_WHALE_MIN_PNL_DROP):
                        logger.warning(
                            "大户净流出 %.0f%% 但价格未跌(pnl=%+.1f%% > %.0f%%) — 暂不熔断 %s",
                            float(dump_meta.get("dump_pct") or 0) * 100,
                            pnl_pct * 100,
                            float(C.EARLY_WHALE_MIN_PNL_DROP) * 100,
                            pos["symbol"],
                        )
                        dump = False
                    # 连续确认：单次读数波动不算，需连续 N 次都判定流出
                    if dump:
                        strikes = int(pos.get("whale_strikes") or 0) + 1
                        pos["whale_strikes"] = strikes
                        need = int(C.EARLY_WHALE_STRIKES)
                        if strikes < need:
                            logger.warning(
                                "大户流出 %.0f%% 第 %d/%d 次（未达连续确认，暂不熔断）%s",
                                float(dump_meta.get("dump_pct") or 0) * 100,
                                strikes,
                                need,
                                pos["symbol"],
                            )
                            dump = False
                    else:
                        pos["whale_strikes"] = 0
                    if dump:
                        pos["whale_dump_done"] = True
                        trade = self._close_partial(pos, 1.0, px, "whale_dump")
                        if not trade:
                            pos["whale_dump_done"] = False
                            continue
                        self._arm_mint_cooldown(
                            mint,
                            seconds=float(C.EARLY_WHALE_COOLDOWN_SEC),
                            reason="whale_dump",
                        )
                        self._record_mint_loss(
                            mint,
                            reason="whale_dump",
                            pnl_sol=(trade or {}).get("pnl_sol"),
                        )
                        events.append(
                            {
                                "type": "whale_dump",
                                "symbol": pos["symbol"],
                                "mint": mint,
                                "price": px,
                                "pnl_pct": pnl_pct,
                                "age_m": age_m,
                                "dump_meta": dump_meta,
                                "trade": trade,
                            }
                        )
                        logger.error(
                            "🚨 WHALE_DUMP %s age=%.0fs pnl=%.1f%% 大户净流出 %.0f%% "
                            "≥ %.0f%% — 闪电熔断清仓（冷却 %.0fs）",
                            pos["symbol"],
                            age_s,
                            pnl_pct * 100,
                            float(dump_meta.get("dump_pct") or 0) * 100,
                            float(C.EARLY_WHALE_DUMP_PCT) * 100,
                            float(C.EARLY_WHALE_COOLDOWN_SEC),
                        )
                        try:
                            journal.record_alert(
                                action="whale_dump",
                                message=(
                                    f"{pos['symbol']} 早期大户净流出 "
                                    f"{float(dump_meta.get('dump_pct') or 0)*100:.0f}%"
                                ),
                                mint=mint,
                                symbol=pos["symbol"],
                                context=dump_meta,
                            )
                        except Exception:
                            pass
                        self.positions.pop(mint, None)
                        continue
                except Exception:
                    logger.exception("早期大户监控异常 %s（本轮跳过）", pos.get("symbol"))

            # ①.5 死盘早砍：默认关（PUMP_DEAD_CUT=0）；活跃度常读成 0 易误砍
            if (
                C.DEAD_CUT_ENABLED
                and C.IS_MOMENTUM
                and (pos.get("track") or "A") == "A"
                and not pos.get("dead_cut_done")
                and age_s >= float(C.DEAD_CUT_SECONDS)
                and peak_pnl < float(C.DEAD_CUT_MIN_PNL)
            ):
                pos["dead_cut_done"] = True
                entry_vol = float(pos.get("volume_m5_sol") or 0)
                cur_vol = entry_vol
                try:
                    from .market_data import lookup_activity

                    act = lookup_activity(mint)
                    cur_vol = float(act.get("volume_m5_sol") or 0)
                except Exception:
                    cur_vol = -1.0  # 无行情时按「骤降未知」仍允许早砍
                vol_floor = max(entry_vol * float(C.DEAD_CUT_VOL_RATIO), C.MIN_VOLUME_M5_SOL * 0.5)
                vol_collapsed = cur_vol < 0 or cur_vol <= vol_floor
                if vol_collapsed:
                    trade = self._close_partial(pos, 1.0, px, "dead_stop")
                    if not trade:
                        pos["dead_cut_done"] = False
                        continue
                    events.append(
                        {
                            "type": "dead_stop",
                            "symbol": pos["symbol"],
                            "mint": mint,
                            "price": px,
                            "pnl_pct": pnl_pct,
                            "peak_pnl": peak_pnl,
                            "age_m": age_m,
                            "entry_vol": entry_vol,
                            "cur_vol": cur_vol,
                            "trade": trade,
                        }
                    )
                    logger.info(
                        "💀 DEAD_STOP %s age=%.0fs peak=+%.1f%% pnl=%.1f%% vol %.2f→%.2f — 僵尸早砍",
                        pos["symbol"],
                        age_s,
                        peak_pnl * 100,
                        pnl_pct * 100,
                        entry_vol,
                        cur_vol,
                    )
                    self._arm_mint_cooldown(mint, reason="dead_stop", entry_ref=entry)
                    self._record_mint_loss(
                        mint,
                        reason="dead_stop",
                        pnl_sol=(trade or {}).get("pnl_sol"),
                    )
                    self.positions.pop(mint, None)
                    continue

            # ② 时间止损：已按用户要求停用（时间到了不再砍仓/不再转保本）
            #    仓位仅由硬止损 / TP1 / 移动止盈 / 死盘早砍 管理。

            # ③ 第一止盈 TP1；tp1_sell≤0 = 纯移动止盈（开仓即挂 trail，不卖仓）
            trail_only = float(xp["tp1_sell"]) <= 0
            if trail_only and not pos.get("tp1_done") and not pos.get("be_takeover"):
                pos["tp1_done"] = True
                pos["trail_only"] = True
                pos["peak"] = max(peak, px)
                pos["trail_line"] = float(pos["peak"]) * (1.0 - float(xp["trail"]))
                logger.info(
                    "TRAIL_ONLY[%s] %s 开仓即跟峰 回撤%.0f%% line=%.8g",
                    pos.get("track") or "A",
                    pos["symbol"],
                    float(xp["trail"]) * 100,
                    pos["trail_line"],
                )
            elif (
                not trail_only
                and not pos.get("tp1_done")
                and not pos.get("be_takeover")
                and pnl_pct >= float(xp["tp1"])
            ):
                # 假涨拦截：盘口到了 TP1，但 Jupiter 可兑现远低于成本 → 全仓紧急逃生，禁止半仓止盈
                cost_left = float(entry) * float(pos.get("qty_left") or 0)
                realizable = pos.get("realizable_sol")
                fake_tp = (
                    realizable is not None
                    and cost_left > 0
                    and float(realizable) < cost_left * float(C.TP1_REALIZABLE_MIN)
                )
                if fake_tp:
                    logger.error(
                        "🚨 假涨 TP1 拦截 %s：盘口 +%.1f%% 但可兑现 %.4f < 成本×%.0f%%=%.4f — 全仓逃生",
                        pos["symbol"],
                        pnl_pct * 100,
                        float(realizable),
                        float(C.TP1_REALIZABLE_MIN) * 100,
                        cost_left * float(C.TP1_REALIZABLE_MIN),
                    )
                    trade = self._close_partial(pos, 1.0, px, "liquidity_escape")
                    if trade:
                        self._arm_mint_cooldown(
                            mint, reason="liquidity_escape", entry_ref=entry
                        )
                        self._record_mint_loss(
                            mint,
                            reason="liquidity_escape",
                            pnl_sol=(trade or {}).get("pnl_sol"),
                        )
                        events.append(
                            {
                                "type": "liquidity_escape",
                                "symbol": pos["symbol"],
                                "mint": mint,
                                "price": px,
                                "pnl_pct": pnl_pct,
                                "realizable": realizable,
                                "trade": trade,
                            }
                        )
                        self.positions.pop(mint, None)
                    continue
                trade = self._close_partial(pos, float(xp["tp1_sell"]), px, "tp1")
                if trade:
                    pos["tp1_done"] = True
                    pos["peak"] = px
                    pos["trail_line"] = px * (1.0 - float(xp["trail"]))
                    events.append(
                        {"type": "tp1", "symbol": pos["symbol"], "mint": mint, "price": px, "pnl_pct": pnl_pct, "trade": trade}
                    )
                    logger.info("TP1[%s] %s @%.8g (+%.1f%%)", pos.get("track") or "A", pos["symbol"], px, pnl_pct * 100)

            # ④ 移动止盈 / 保本止损
            if pos.get("tp1_done") or pos.get("be_takeover"):
                trail_line = float(pos.get("trail_line") or 0)
                if pos.get("be_takeover") and not pos.get("tp1_done"):
                    # ⚠️ 这条分支目前进不来：be_takeover 全仓库无人写入（随②时间止损
                    # 一起停用），be_price 同样从未落盘。重新启用是风险决策，不在此处
                    # 顺手打开；但若要启用，be_price 必须按 mark 口径写（等于 basis
                    # 或 basis×(1+费用)），绝不能塞 entry——那是成交价口径，会把保本线
                    # 整体抬高一个滑点+手续费的楔子，保本止损跟着提前开火。
                    be_floor = float(pos.get("be_price") or basis)
                    eff_line = max(trail_line, be_floor)
                    exit_reason = "be_stop"
                else:
                    eff_line = trail_line
                    exit_reason = "trail_stop"
                if eff_line > 0 and px <= eff_line:
                    trade = self._close_partial(pos, 1.0, px, exit_reason)
                    if not trade:
                        continue
                    events.append({"type": exit_reason, "symbol": pos["symbol"], "mint": mint, "price": px, "pnl_pct": pnl_pct, "trade": trade})
                    logger.info("%s %s @%.8g line=%.8g (%.1f%%)", exit_reason.upper(), pos["symbol"], px, eff_line, pnl_pct * 100)
                    self._arm_mint_cooldown(mint, reason=exit_reason, entry_ref=entry)
                    self._record_mint_loss(
                        mint,
                        reason=exit_reason,
                        pnl_sol=(trade or {}).get("pnl_sol"),
                    )
                    self.positions.pop(mint, None)
                    continue

            if float(pos.get("qty_left") or 0) <= 1e-18:
                self.positions.pop(mint, None)

        if events:
            self._persist_positions()
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
            # pnl_pct：账本口径（成交价 → 现价），与 unrealized_pnl_sol 一致
            pnl_pct = ((mark - entry) / entry * 100.0) if entry > 0 else 0.0
            # pnl_pct_mark：出场阶梯真正用的口径（mark 对 mark），两者不同就说明
            # 成交价与链上标价存在基差，看板要能看出来
            basis = mark_basis(pos)
            pnl_pct_mark = ((mark - basis) / basis * 100.0) if basis > 0 else 0.0
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
                    "entry_mark": pos.get("entry_mark"),
                    "pnl_pct_mark": round(pnl_pct_mark, 4),
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
                    "track": pos.get("track") or "A",
                    "dry_run": pos.get("dry_run"),
                    "shadow": bool(pos.get("shadow")),
                    "pool": pos.get("pool"),
                    "dex": pos.get("dex"),
                    "price_source": pos.get("price_source"),
                    "price_ts": pos.get("price_ts"),
                    # mark 是否已经不动了：看板必须能一眼看出这仓在盲飞
                    "mark_stale_sec": (
                        round(time.time() - float(pos["mark_stale_since"]), 1)
                        if pos.get("mark_stale_since")
                        else 0.0
                    ),
                    "mark_stale_reason": pos.get("mark_stale_reason"),
                    "max_float_pnl_pct": pos.get("max_float_pnl_pct"),
                    "sol_spent": pos.get("sol_spent"),
                    "fees_sol": pos.get("fees_sol"),
                    "gas_sol": pos.get("gas_sol"),
                    "slippage_sol": pos.get("slippage_sol"),
                }
            )
        return rows

    @staticmethod
    def _pos_value(pos: dict[str, Any]) -> float:
        """单仓市值：盘口价与「Jupiter 可兑现值」取小。

        池子被抽干时盘口价会虚高（vault 比例失真），只有能换回的 SOL 才算数。
        """
        nominal = float(pos.get("qty_left") or 0) * float(pos.get("mark") or pos.get("entry") or 0)
        realizable = pos.get("realizable_sol")
        if realizable is None:
            return nominal
        return min(nominal, max(0.0, float(realizable)))

    def unrealized_pnl(self) -> float:
        total = 0.0
        for pos in self.positions.values():
            cost = float(pos["entry"]) * float(pos["qty_left"])
            total += self._pos_value(pos) - cost
        return total

    def position_value(self) -> float:
        return sum(self._pos_value(pos) for pos in self.positions.values())

    def write_off_dust_positions(self) -> list[dict[str, Any]]:
        """可兑现价值已不足 gas 成本的仓位（抽池/rug）→ 先强卖一次，仍不行再计损核销。"""
        written: list[dict[str, Any]] = []
        for mint, pos in list(self.positions.items()):
            if pos.get("shadow") or pos.get("dry_run"):
                continue
            realizable = pos.get("realizable_sol")
            if realizable is None or float(realizable) > float(C.DUST_WRITEOFF_SOL):
                continue
            # 核销前最后一搏：urgent salvage（哪怕只收回 dust 级 SOL 也好过账面归零）
            if not pos.get("_dust_salvage_tried"):
                pos["_dust_salvage_tried"] = True
                px = float(pos.get("mark") or pos.get("entry") or 0) or 1e-12
                trade = self._close_partial(pos, 1.0, px, "liquidity_escape")
                if trade:
                    self._arm_mint_cooldown(
                        mint, reason="liquidity_escape", entry_ref=float(pos.get("entry") or 0)
                    )
                    self._record_mint_loss(
                        mint, reason="liquidity_escape", pnl_sol=trade.get("pnl_sol")
                    )
                    self.positions.pop(mint, None)
                    written.append(trade)
                    logger.warning(
                        "🧹 dust salvage 成功 %s pnl=%+.6f",
                        pos.get("symbol"),
                        float(trade.get("pnl_sol") or 0),
                    )
                    continue
            cost = float(pos["entry"]) * float(pos["qty_left"])
            # 核销＝放弃该袋代币（不卖出，卖出回款还不够 gas），按全损入账
            loss = -cost
            self.gross_realized += loss
            self.realized_pnl = self.net_realized()
            self.positions.pop(mint, None)
            logger.error(
                "🚨 核销无流动性仓位 %s：成本 %.6f SOL，可兑现仅 %.6f SOL（不够 gas）→ 全损入账 %+.6f SOL",
                pos.get("symbol"), cost, float(realizable), loss,
            )
            trade = journal.record_trade(
                action="write_off",
                mint=mint,
                symbol=pos.get("symbol"),
                amount_sol=0.0,
                price=0.0,
                pnl_sol=loss,
                pnl_percent=(loss / cost * 100.0) if cost > 0 else None,
                exit_reason="流动性坍塌核销（池子被抽干，代币无法卖出）",
                position_id=pos.get("id"),
                dry_run=False,
                shadow=False,
            )
            self._record_mint_loss(mint, reason="write_off", pnl_sol=loss)
            written.append(trade or {})
        if written:
            self._persist_account()
            self._persist_positions()
        return written

    def flatten_all(
        self,
        *,
        reason: str = "manual_flatten",
        mint: str | None = None,
    ) -> list[dict[str, Any]]:
        """手动全平：逐仓按市价卖光，失败的仓位保留在账面等重试。

        mint 非空时只平该仓。
        """
        closed: list[dict[str, Any]] = []
        failed: list[str] = []
        target = (mint or "").strip() or None
        items = list(self.positions.items())
        if target:
            items = [(m, p) for m, p in items if m == target]
            if not items:
                logger.warning("手动清仓：未找到持仓 mint=%s…", target[:8])
                return []
        for mint_key, pos in items:
            px = float(pos.get("mark") or pos.get("entry") or 0)
            if px <= 0:
                logger.error("清仓跳过 %s：无有效标记价", pos.get("symbol") or mint_key)
                failed.append(mint_key)
                continue
            trade = self._close_partial(pos, 1.0, px, reason)
            if not trade:
                failed.append(mint_key)
                logger.error("清仓失败 %s mint=%s…", pos.get("symbol"), mint_key[:8])
                continue
            self.positions.pop(mint_key, None)
            self._arm_mint_cooldown(
                mint_key, reason=reason, entry_ref=float(pos.get("entry") or 0)
            )
            if float(trade.get("pnl_sol") or 0) < 0:
                self._record_mint_loss(
                    mint_key, reason=reason, pnl_sol=trade.get("pnl_sol")
                )
            closed.append(trade)
            logger.warning(
                "🧹 手动清仓 %s pnl=%+.4f SOL sig=%s",
                pos.get("symbol"),
                float(trade.get("pnl_sol") or 0),
                (trade.get("tx_signature") or trade.get("signature") or "")[:16],
            )
        self._persist_positions()
        self._persist_account()
        if failed:
            logger.error(
                "清仓未完成 %d/%d：%s",
                len(failed),
                len(failed) + len(closed),
                failed,
            )
        return closed

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
        """实盘会话重置：切断纸面账本，只用链上余额作本金/现金。

        注意：真实持仓（链上已成交）必须保留，否则重启后会重复买入且旧仓失去止损托管。
        本金按「链上 SOL + 在仓市值」计，避免把持仓成本算成凭空盈利。
        """
        bal = max(0.0, float(sol_balance))
        self.cash = bal
        self.total_fees = 0.0
        self.total_slippage = 0.0
        self.total_gas = 0.0
        # 丢弃纸面/影子残留仓位；真实仓位保留续管
        for mint, pos in list(self.positions.items()):
            if pos.get("shadow") or pos.get("dry_run", True):
                self.positions.pop(mint, None)
        # 买入花掉的 SOL 变成了库存而非亏损，因此已实现盈亏要按「成本基准」还原，
        # 否则持仓平掉时它的盈亏会被重复计一次。浮盈浮亏留给 unrealized_pnl。
        cost_basis = sum(
            float(p.get("entry") or 0) * float(p.get("qty_left") or 0)
            for p in self.positions.values()
        )

        if self.live_bankroll_anchor and self.live_bankroll_anchor > 0:
            # 已有本金锚点：保留跨重启收益率，不再清零
            self.bankroll = float(self.live_bankroll_anchor)
            self.gross_realized = (bal + cost_basis) - self.bankroll
            self.realized_pnl = self.gross_realized
            logger.info(
                "LIVE 沿用本金锚点 bankroll=%.6f cash=%.6f 在仓成本=%.6f 已实现=%+.6f 续管仓位=%d",
                self.bankroll,
                bal,
                cost_basis,
                self.gross_realized,
                len(self.positions),
            )
        else:
            self.gross_realized = 0.0
            self.realized_pnl = 0.0
            self.bankroll = bal + cost_basis
            self.live_bankroll_anchor = self.bankroll
            logger.info(
                "LIVE 会话账户已初始化 bankroll=现金+在仓成本=%.6f（首次锚定，后续重启不再清零）",
                self.bankroll,
            )

        self.last_audit = {
            "ok": True,
            "skipped": True,
            "reason": "live_session_reset",
        }
        self._persist_account()
        self._persist_positions()

    def sync_live_balance(self, sol_balance: float, *, read_at: float | None = None) -> None:
        """实盘现金强制对齐链上 SOL 余额，避免纸面账本漂移。

        持仓是 SPL 代币，不占用 SOL 余额，所以无论空仓与否 cash 都应当等于链上 SOL，
        仓位市值单独由 position_value() 计入权益。买入花掉的 SOL 变成库存而非亏损，
        因此已实现按「链上 SOL + 在仓成本 − 本金」还原，口径与 reset_live_session 一致。

        read_at 为发起余额查询时的 monotonic 时间；若查询期间又有成交，读数已过期，
        直接丢弃等下一轮，否则会把成交前的余额写回账户。
        """
        if self.dry_run or self.shadow:
            return
        if read_at is not None and self._last_fill_mono > read_at:
            return
        bal = max(0.0, float(sol_balance))
        cost_basis = sum(
            float(p.get("entry") or 0) * float(p.get("qty_left") or 0)
            for p in self.positions.values()
        )
        self.cash = bal
        self.gross_realized = (bal + cost_basis) - float(self.bankroll)
        self.realized_pnl = self.gross_realized
        # 链上口径已含手续费/滑点/gas，再减一次会重复计
        self.total_fees = 0.0
        self.total_slippage = 0.0
        self.total_gas = 0.0
        self._persist_account()

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
