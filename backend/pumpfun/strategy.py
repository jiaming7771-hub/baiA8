"""策略过滤引擎：默认「顺势接力 / 动量突破」，旧捡尸逻辑仅兼容保留。"""

from __future__ import annotations

import hashlib
import logging
import math
import random
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from . import config as C

logger = logging.getLogger("pumpfun.strategy")


@dataclass
class Candidate:
    mint: str
    symbol: str
    listed_at: float  # unix ts
    ath_price: float
    price: float
    buy_vol: float  # m15 买单数（dip 恐慌用）
    sell_vol: float  # m15 卖单数
    whale_dump_pct: float  # 0~1（dip）
    liquidity_sol: float = 0.0
    tx_count_m5: int = 0
    volume_m5_sol: float = 0.0
    volume_m5_usd: float = 0.0
    pool: str | None = None
    dex: str | None = None
    # —— 动量字段 ——
    buys_m5: int = 0
    sells_m5: int = 0
    chg_m5: float = 0.0  # %
    chg_m15: float = 0.0
    chg_m30: float = 0.0
    # 数据源是否真给了 m15/m30；False 表示是 m5/h1 顶替值，不可当真实窗口
    chg_m15_real: bool = False
    chg_m30_real: bool = False
    # —— 自采价格序列导出量（不受 Gecko 429 / Dex 缺 m15 影响）——
    self_low: float = 0.0
    self_high: float = 0.0
    self_span_min: float = 0.0  # 序列覆盖时长（分钟）
    self_points: int = 0
    self_px_15m_ago: float = 0.0  # 15 分钟前的自采价；0=序列还不够老
    price_streak: int = 0  # 最近扫描连续上涨次数
    # —— 防伪：真实 K 线（Gecko OHLCV）与行情新鲜度 ——
    ohlcv_low: float = 0.0
    ohlcv_high: float = 0.0
    ohlcv_ok: bool = False
    data_ts: float = 0.0  # 观察池最近刷新 unix ts；0=未知
    volume_h1_sol: float = 0.0
    max_drawdown_seen: float = 0.0  # 观察期内见过的最大回撤 0~1
    track: str | None = None  # A / B / dip

    @property
    def age_minutes(self) -> float:
        return max(0.0, (time.time() - self.listed_at) / 60.0)

    @property
    def peak_price(self) -> float:
        """有效峰值：永远 ≥ 现价（脏 ATH / 观察池错乱时自愈）。"""
        return max(
            float(self.ath_price or 0),
            float(self.ohlcv_high or 0) if self.ohlcv_ok else 0.0,
            float(self.self_high or 0) if self.self_hist_usable else 0.0,
            float(self.price or 0),
        )

    @property
    def drawdown(self) -> float:
        """回撤 = (现价 - 峰值) / 峰值，严格夹在 [-1.0, 0.0]。

        现价高于峰值 → 0（无回撤）；现价归零 → -1（-100%）。
        绝不容许正数或 < -100%。
        """
        peak = self.peak_price
        px = float(self.price or 0)
        if peak <= 0 or px <= 0:
            return 0.0
        return max(-1.0, min(0.0, (px / peak) - 1.0))

    @property
    def ath_drop(self) -> float:
        """距峰值跌幅 0~1（= -drawdown），兼容旧捡尸逻辑。"""
        return -self.drawdown

    @property
    def pullback(self) -> float:
        """距短期高点回撤幅度 0~1（过滤用正数）。优先 OHLCV high，其次自采 high。"""
        if self.ohlcv_ok and self.ohlcv_high > 0 and self.price > 0:
            high = max(float(self.ohlcv_high), float(self.price))
            return max(0.0, min(1.0, 1.0 - (self.price / high)))
        if self.self_hist_usable and self.self_high > 0 and self.price > 0:
            high = max(float(self.self_high), float(self.price))
            return max(0.0, min(1.0, 1.0 - (self.price / high)))
        return self.ath_drop

    @property
    def panic_ratio(self) -> float:
        """旧捡尸：卖/买（m15）。"""
        if self.buy_vol <= 0:
            return 999.0 if self.sell_vol > 0 else 0.0
        return self.sell_vol / self.buy_vol

    @property
    def buy_sell_ratio(self) -> float:
        """动量：买/卖笔数比（m5）。"""
        buys = self.buys_m5 if self.buys_m5 > 0 else 0
        sells = self.sells_m5
        if sells <= 0:
            return 999.0 if buys > 0 else 0.0
        return buys / sells

    @property
    def self_hist_usable(self) -> bool:
        """自采序列是否够格当真实低点用（覆盖够久 + 点数够多）。"""
        return (
            self.self_low > 0
            and self.self_points >= int(C.REBOUND_SELF_MIN_POINTS)
            and self.self_span_min >= float(C.REBOUND_SELF_MIN_SPAN_MIN)
        )

    @property
    def ohlcv_low_trustworthy(self) -> bool:
        """OHLCV 低点是否可信——拿自采序列当独立证据交叉校验。

        数据源对刚建池的盘会返回近零低点（实测 CAGE 回升 105122%、VORF 30551%），
        而这些币的自采序列是好的。无条件优先 OHLCV 会让垃圾低点顶掉可信数据、
        再被回升上限拒掉，等于凭空误杀。

        自采窗口通常短于 OHLCV 窗口，真实低点本就可能更低，故倍数放得很宽；
        只拦那种差着数量级的。没有自采序列可比时不作判断（交给回升上限兜）。
        """
        if not (self.ohlcv_ok and self.ohlcv_low > 0):
            return False
        if not self.self_hist_usable:
            return True
        return self.ohlcv_low >= self.self_low / float(C.REBOUND_OHLCV_MAX_SELF_RATIO)

    @property
    def rebound_src(self) -> str:
        """回升数值的来源，供过滤/看板审计：ohlcv / self / window / none。"""
        if self.ohlcv_low_trustworthy and self.price > 0:
            return "ohlcv"
        if self.self_hist_usable and self.price > 0:
            return "self"
        real = [
            c
            for c, ok in ((self.chg_m15, self.chg_m15_real), (self.chg_m30, self.chg_m30_real))
            if ok and c > 0
        ]
        return "window" if real else "none"

    @property
    def rebound(self) -> float:
        """从近期低点回升幅度（小数）。只认可信来源，绝不吃顶替值。

        优先级：OHLCV 真实 low → 自采序列低点 → 数据源真给的 m15/m30 窗口 → 0。

        历史坑：Dexscreener 不返回 m15/m30，旧代码用 m5/h1 顶替后反推，
        使「回升」实际等于 5m 涨幅（和过热追高过滤器抢同一个变量）。
        """
        src = self.rebound_src
        if src == "ohlcv":
            return max(0.0, (self.price / self.ohlcv_low) - 1.0)
        if src == "self":
            return max(0.0, (self.price / self.self_low) - 1.0)
        if src == "window":
            positives = [
                c / 100.0
                for c, ok in (
                    (self.chg_m15, self.chg_m15_real),
                    (self.chg_m30, self.chg_m30_real),
                )
                if ok and c > 0
            ]
            return min(positives) if positives else 0.0
        return 0.0

    @property
    def wick_base_pct(self) -> float:
        """插针检测的分母：15 分钟窗口涨幅(%)，只用可信来源，无来源返回 0。

        旧实现用 max(chg_m15, chg_m30)，而 chg_m15 恒等于 chg_m5，
        导致比值恒 ≤ 1.0、该检测永不触发。
        """
        if self.self_px_15m_ago > 0 and self.price > 0:
            return (self.price / self.self_px_15m_ago - 1.0) * 100.0
        reals = [
            c
            for c, ok in (
                (self.chg_m15, self.chg_m15_real),
                (self.chg_m30, self.chg_m30_real),
            )
            if ok
        ]
        return max(reals) if reals else 0.0

    def to_row(self) -> dict[str, Any]:
        return {
            "mint": self.mint,
            "symbol": self.symbol,
            "listed_at": datetime.fromtimestamp(self.listed_at, tz=timezone.utc).isoformat(),
            "age_minutes": round(self.age_minutes, 1),
            "ath_drop_pct": round(self.ath_drop * 100, 2),
            # 看板回撤：有符号百分比，严格 ∈ [-100, 0]
            "drawdown_pct": round(self.drawdown * 100, 2),
            "pullback_pct": round(self.drawdown * 100, 2),
            "rebound_pct": round(self.rebound * 100, 2),
            "rebound_src": self.rebound_src,
            "self_span_min": round(self.self_span_min, 1),
            "self_points": self.self_points,
            "panic_ratio": round(self.panic_ratio, 2),
            "buy_sell_ratio": round(self.buy_sell_ratio, 2),
            "whale_dump_pct": round(self.whale_dump_pct * 100, 1),
            "tx_count_m5": self.tx_count_m5,
            "volume_m5_sol": round(self.volume_m5_sol, 3),
            "volume_m5_usd": round(self.volume_m5_usd, 2),
            "chg_m5": round(self.chg_m5, 2),
            "chg_m15": round(self.chg_m15, 2),
            "chg_m30": round(self.chg_m30, 2),
            "price_streak": self.price_streak,
            "buys_m5": self.buys_m5,
            "sells_m5": self.sells_m5,
            "price": self.price,
            "ath_price": self.peak_price,
            "liquidity_sol": round(self.liquidity_sol, 3),
            "pool": self.pool,
            "dex": self.dex,
            "ohlcv_ok": self.ohlcv_ok,
            "volume_h1_sol": round(self.volume_h1_sol, 3),
            "max_drawdown_seen": round(self.max_drawdown_seen * 100, 1),
            "track": self.track,
            "strategy_mode": C.STRATEGY_MODE,
        }


def _age_violent_exempt(c: Candidate, *, age_m: float, bs: float, vol_m5: float) -> bool:
    """老盘暴力豁免：超 A 轨龄但 5m 巨量 + 极强买压 → 仍可走 A。"""
    if age_m <= C.TRACK_A_AGE_MAX:
        return False
    return (
        vol_m5 >= C.AGE_EXEMPT_VOLUME_M5_SOL
        and c.tx_count_m5 >= C.AGE_EXEMPT_TX_M5
        and bs >= round(C.AGE_EXEMPT_BUY_SELL_RATIO, 2)
    )


def _shared_gate_fails(c: Candidate) -> list[str]:
    """双轨共享底线：过新 / 过旧 / 插针 / 砸盘 / 价格无效。"""
    fails: list[str] = []
    age_m = round(c.age_minutes, 1)
    # 开盘/迁移最初的窗口最脏（拉砸+抽池），一律避开；双轨底线取更严的那个
    floor = min(float(C.TRACK_A_AGE_MIN), float(C.TRACK_B_AGE_MIN)) if C.TRACK_B_ENABLED else float(C.TRACK_A_AGE_MIN)
    floor = max(floor, float(C.AGE_MIN_MINUTES))
    if C.IS_MOMENTUM and age_m < floor:
        fails.append(f"上线 {age_m:.0f}m < {floor:.0f}m（开盘脏窗口）")
    pullback_pct = round(c.pullback * 100, 1)
    if c.data_ts > 0:
        age_sec = time.time() - c.data_ts
        if age_sec > float(C.SIGNAL_MAX_AGE_SEC):
            fails.append(
                f"行情过旧 {age_sec:.0f}s > {C.SIGNAL_MAX_AGE_SEC:.0f}s（代理/限流误判风险）"
            )
    crash_pct = round(float(C.CRASH_PULLBACK_MAX) * 100, 1)
    if pullback_pct > crash_pct:
        fails.append(
            f"砸盘残废回撤 {pullback_pct:.1f}% > {crash_pct:.0f}%（一票否决）"
        )
    mdd_pct = round(float(C.MDD_BLACKLIST_PCT) * 100, 1)
    # 观察期内见过的最大回撤（若有）或当前回撤触及历史腰斩线
    hist = float(getattr(c, "max_drawdown_seen", 0) or 0)
    if max(hist, c.pullback) >= float(C.MDD_BLACKLIST_PCT):
        fails.append(
            f"历史最大回撤 {max(hist, c.pullback)*100:.0f}% ≥ {mdd_pct:.0f}%（永久拉黑级）"
        )
    # 5m 涨幅窗口：动能不足不进、冲太猛不追
    chg5 = round(float(c.chg_m5 or 0), 2)
    if chg5 < float(C.ENTRY_CHG_M5_MIN):
        fails.append(f"5m涨幅 {chg5:.2f}% < {C.ENTRY_CHG_M5_MIN:.0f}%（动能不足）")
    elif chg5 > float(C.ENTRY_CHG_M5_MAX):
        fails.append(f"5m涨幅 {chg5:.2f}% > {C.ENTRY_CHG_M5_MAX:.0f}%（过热追高）")
    # 只买已毕业池：曲线盘可被一键抽干（Bubsem -87% 跑路盘）
    if C.IS_MOMENTUM and C.ENTRY_GRADUATED_ONLY:
        dex = (c.dex or "").lower()
        if "swap" not in dex:
            fails.append("未毕业曲线盘（graduated-only：防一键抽池跑路）")
    # 动量：相对峰值回撤过大 = 残盘，禁买（Found/ANONSEM 类）
    if C.IS_MOMENTUM:
        ath_pct = round(float(c.ath_drop) * 100, 1)
        max_ath = round(float(C.ENTRY_ATH_DROP_MAX) * 100, 1)
        if ath_pct > max_ath:
            fails.append(f"ATH回撤 {ath_pct:.1f}% > {max_ath:.0f}%（残盘禁买）")
    if c.price <= 0 or c.peak_price <= 0:
        fails.append("价格无效")
    return fails


def pass_track_a_filters(c: Candidate) -> tuple[bool, list[str]]:
    """轨道 A：短线爆发（3–120m，轻度放宽）。"""
    fails = _shared_gate_fails(c)
    # 数据去伪：无真实序列（OHLCV 或自采）时，禁止仅凭 m5 代理 m15/m30 的"假连续"过关。
    # 可用的自采序列本身已是真实观察，满足「要真数据」；否则才要求自采连涨 ≥N。
    if C.ENTRY_REQUIRE_OHLCV and not (c.ohlcv_ok or c.self_hist_usable):
        need = int(C.ENTRY_MIN_STREAK_NO_OHLCV)
        if c.price_streak < need:
            fails.append(
                f"无真实K线且自采连涨 {c.price_streak} < {need}（禁凭代理窗口入场）"
            )
    age_m = round(c.age_minutes, 1)
    rebound_pct = round(c.rebound * 100, 1)
    pullback_pct = round(c.pullback * 100, 1)
    bs = round(c.buy_sell_ratio, 2)
    vol_m5 = round(c.volume_m5_sol, 3)
    liq = round(c.liquidity_sol, 1)
    chg5 = round(c.chg_m5, 2)

    # 插针假反弹：分母必须是可信的 15m 窗口，拿不到就不判（不能用顶替值假过关）
    base_win = c.wick_base_pct
    if c.chg_m5 > 0 and base_win > 0 and (c.chg_m5 / base_win) > float(C.WICK_SPIKE_RATIO):
        fails.append(
            f"疑似插针假反弹（5m涨{c.chg_m5:.1f}% / 15m窗口{base_win:.1f}% "
            f"> {C.WICK_SPIKE_RATIO}x）"
        )
    # 年龄 <15m 只要求 m15>0；更老要求双窗口（无 OHLCV 时）
    if not c.ohlcv_ok:
        if age_m < 15:
            if c.chg_m15 <= 0:
                fails.append(f"短龄盘 m15={c.chg_m15:.1f}% 未转正")
        elif c.chg_m15 <= 0 or c.chg_m30 <= 0:
            fails.append(
                f"双窗口未同步转正（m15={c.chg_m15:.1f}% m30={c.chg_m30:.1f}%）"
            )

    if pullback_pct > round(C.TRACK_A_PULLBACK_MAX * 100, 1):
        fails.append(
            f"[A]高位回撤 {pullback_pct:.1f}% > {C.TRACK_A_PULLBACK_MAX*100:.0f}%"
        )

    if age_m < C.TRACK_A_AGE_MIN:
        fails.append(f"[A]上线 {age_m:.0f}m < {C.TRACK_A_AGE_MIN:.0f}m")
    elif age_m > C.TRACK_A_AGE_MAX:
        if not _age_violent_exempt(c, age_m=age_m, bs=bs, vol_m5=vol_m5):
            fails.append(f"[A]上线 {age_m:.0f}m > {C.TRACK_A_AGE_MAX:.0f}m")

    if c.rebound_src == "none":
        fails.append(
            f"回升无可信来源（无真K线，自采序列仅 {c.self_span_min:.0f}m/"
            f"{c.self_points}点，需 ≥{C.REBOUND_SELF_MIN_SPAN_MIN:.0f}m"
            f"/{C.REBOUND_SELF_MIN_POINTS}点）"
        )
    elif rebound_pct < round(C.TRACK_A_REBOUND_MIN * 100, 1):
        fails.append(
            f"[A]回升 {rebound_pct:.1f}% < {C.TRACK_A_REBOUND_MIN*100:.0f}%"
        )
    if rebound_pct > round(C.TRACK_A_REBOUND_MAX * 100, 1):
        fails.append(
            f"[A]回升 {rebound_pct:.1f}% > {C.TRACK_A_REBOUND_MAX*100:.0f}%"
        )
    elif rebound_pct > round(C.REBOUND_STRICT_FROM * 100, 1):
        if bs < round(C.REBOUND_STRICT_BUY_SELL, 2):
            fails.append(
                f"[A]延伸段买/卖 {bs:.2f} < {C.REBOUND_STRICT_BUY_SELL}"
            )
        if pullback_pct > round(C.REBOUND_STRICT_PULLBACK * 100, 1):
            fails.append(
                f"[A]延伸段回撤 {pullback_pct:.1f}% > "
                f"{C.REBOUND_STRICT_PULLBACK*100:.0f}%"
            )

    if chg5 <= 0:
        fails.append(f"[A]近5m涨幅 {chg5:.2f}% ≤ 0")
    if c.buys_m5 < c.sells_m5:
        fails.append(f"[A]买盘 {c.buys_m5} < 卖盘 {c.sells_m5}")
    if c.price_streak < C.MOMENTUM_STREAK_MIN:
        fails.append(f"[A]连续上涨 {c.price_streak} < {C.MOMENTUM_STREAK_MIN}")
    if bs < round(C.TRACK_A_BUY_SELL_MIN, 2):
        fails.append(f"[A]买/卖 {bs:.2f} < {C.TRACK_A_BUY_SELL_MIN}")
    if c.tx_count_m5 < C.TRACK_A_MIN_TX_M5:
        fails.append(f"[A]5m成交 {c.tx_count_m5} < {C.TRACK_A_MIN_TX_M5}")
    if vol_m5 < C.TRACK_A_MIN_VOL_M5:
        fails.append(f"[A]5m额 {vol_m5:.2f} < {C.TRACK_A_MIN_VOL_M5}")
    if liq < C.TRACK_A_LIQ_MIN:
        fails.append(f"[A]流动性 {liq:.1f} < {C.TRACK_A_LIQ_MIN}")

    return (len(fails) == 0, fails)


def pass_track_b_filters(c: Candidate) -> tuple[bool, list[str]]:
    """轨道 B：趋势蓄势/老盘贴近高点放量（120m–1440m）。

    首版用现有窗口近似「箱体上沿 + 放量」：回撤很浅 + 5m/15m 转正 +
    5m 成交折算 ≥ h1 成交的 TRACK_B_VOL_SPIKE_RATIO 倍节奏。
    """
    fails = _shared_gate_fails(c)
    age_m = round(c.age_minutes, 1)
    pullback_pct = round(c.pullback * 100, 1)
    bs = round(c.buy_sell_ratio, 2)
    vol_m5 = round(c.volume_m5_sol, 3)
    vol_h1 = round(float(getattr(c, "volume_h1_sol", 0) or 0), 3)
    liq = round(c.liquidity_sol, 1)
    chg5 = round(c.chg_m5, 2)

    if age_m < C.TRACK_B_AGE_MIN:
        fails.append(f"[B]上线 {age_m:.0f}m < {C.TRACK_B_AGE_MIN:.0f}m（未入老盘窗）")
    if age_m > C.TRACK_B_AGE_MAX:
        fails.append(f"[B]上线 {age_m:.0f}m > {C.TRACK_B_AGE_MAX:.0f}m")

    if liq < C.TRACK_B_LIQ_MIN:
        fails.append(f"[B]流动性 {liq:.1f} < {C.TRACK_B_LIQ_MIN}")

    # 贴近箱体上沿
    if pullback_pct > round(C.TRACK_B_PULLBACK_MAX * 100, 1):
        fails.append(
            f"[B]回撤 {pullback_pct:.1f}% > {C.TRACK_B_PULLBACK_MAX*100:.0f}%（未破上沿）"
        )

    if chg5 <= 0:
        fails.append(f"[B]近5m涨幅 {chg5:.2f}% ≤ 0（未突破）")
    if c.chg_m15 <= 0:
        fails.append(f"[B]近15m涨幅 {c.chg_m15:.1f}% ≤ 0")

    if c.buys_m5 < c.sells_m5:
        fails.append(f"[B]买盘 {c.buys_m5} < 卖盘 {c.sells_m5}")
    if bs < round(C.TRACK_B_BUY_SELL_MIN, 2):
        fails.append(f"[B]买/卖 {bs:.2f} < {C.TRACK_B_BUY_SELL_MIN}")

    if c.tx_count_m5 < C.TRACK_B_MIN_TX_M5:
        fails.append(f"[B]5m成交 {c.tx_count_m5} < {C.TRACK_B_MIN_TX_M5}")
    if vol_m5 < C.TRACK_B_MIN_VOL_M5:
        fails.append(f"[B]5m额 {vol_m5:.2f} < {C.TRACK_B_MIN_VOL_M5}")

    # 放量：5m×12 推到小时节奏 vs 实际 h1
    if vol_h1 > 0:
        pace_1h = vol_m5 * 12.0
        need = vol_h1 * float(C.TRACK_B_VOL_SPIKE_RATIO)
        if pace_1h < need:
            fails.append(
                f"[B]放量不足 5m节奏{pace_1h:.1f} < h1×{C.TRACK_B_VOL_SPIKE_RATIO}"
                f"={need:.1f}"
            )
    else:
        # 无 h1 时要求更强 5m 绝对量
        if vol_m5 < C.TRACK_B_MIN_VOL_M5 * 1.5:
            fails.append(f"[B]无h1量，5m额需≥{C.TRACK_B_MIN_VOL_M5*1.5:.0f}")

    return (len(fails) == 0, fails)


def pass_momentum_filters(c: Candidate) -> tuple[bool, list[str]]:
    """兼容旧调用：等价于只跑轨道 A。"""
    return pass_track_a_filters(c)


def pass_dip_filters(c: Candidate) -> tuple[bool, list[str]]:
    """旧黄金猎杀（捡尸）过滤 — 仅 STRATEGY_MODE=dip 时启用。"""
    fails: list[str] = []
    age_m = round(c.age_minutes, 1)
    ath_pct = round(c.ath_drop * 100, 1)
    panic = round(c.panic_ratio, 2)
    whale_pct = round(c.whale_dump_pct * 100, 0)
    vol_m5 = round(c.volume_m5_sol, 3)
    liq = round(c.liquidity_sol, 1)

    if not (C.AGE_MIN_MINUTES <= age_m <= C.AGE_MAX_MINUTES):
        fails.append(
            f"上线时长 {age_m:.0f}m ∉ [{C.AGE_MIN_MINUTES:.0f},{C.AGE_MAX_MINUTES:.0f}]"
        )
    if ath_pct < round(C.ATH_DROP_MIN * 100, 1):
        fails.append(f"ATH跌幅 {ath_pct:.1f}% < {C.ATH_DROP_MIN*100:.0f}%（超跌不足）")
    if ath_pct > round(C.ATH_DROP_MAX * 100, 1):
        fails.append(f"ATH跌幅 {ath_pct:.1f}% > {C.ATH_DROP_MAX*100:.0f}%（疑似归零死币）")
    if panic < round(C.PANIC_RATIO_MIN, 2):
        fails.append(f"恐慌比 {panic:.2f} < {C.PANIC_RATIO_MIN}")
    if whale_pct < round(C.WHALE_DUMP_MIN * 100, 0):
        fails.append(f"单户清仓 {whale_pct:.0f}% < {C.WHALE_DUMP_MIN*100:.0f}%")
    if c.tx_count_m5 < C.MIN_TX_M5:
        fails.append(f"近5m成交 {c.tx_count_m5} 笔 < {C.MIN_TX_M5}（交易冻结）")
    if vol_m5 < C.MIN_VOLUME_M5_SOL:
        fails.append(
            f"近5m成交额 {vol_m5:.2f} SOL < {C.MIN_VOLUME_M5_SOL:.1f}（成交枯竭）"
        )
    if liq < C.LIQUIDITY_MIN_SOL:
        fails.append(f"流动性 {liq:.1f} SOL < {C.LIQUIDITY_MIN_SOL:.0f}（盘口枯竭）")
    if c.price <= 0 or c.ath_price <= 0:
        fails.append("价格无效")
    return (len(fails) == 0, fails)


def classify_track(c: Candidate) -> tuple[str | None, list[str]]:
    """返回 (track, fails)。优先 A；A 不过再试 B。"""
    if not C.IS_MOMENTUM:
        ok, fails = pass_dip_filters(c)
        return (("dip" if ok else None), fails)

    ok_a, fails_a = pass_track_a_filters(c)
    if ok_a:
        return "A", []
    if C.TRACK_B_ENABLED:
        ok_b, fails_b = pass_track_b_filters(c)
        if ok_b:
            return "B", []
        # 展示用：年龄落在 B 窗则给 B 原因，否则 A
        if C.TRACK_B_AGE_MIN <= c.age_minutes <= C.TRACK_B_AGE_MAX:
            return None, fails_b
    return None, fails_a


def pass_hard_filters(c: Candidate) -> tuple[bool, list[str]]:
    track, fails = classify_track(c)
    return (track is not None, fails)


def activity_score(tx_count_m5: float, volume_m5_sol: float) -> float:
    """近 5m 活跃度 0~1：取 tx / 成交额相对门槛倍数的短板，再压对数刻度。

    用对数而非线性，因为活跃度的边际信息随倍数递减 —— 10 倍门槛与 1 倍门槛的差别
    远大于 1000 倍与 100 倍的差别。单调递增，无甜点区：刚过门槛得 0，
    ACTIVITY_MULT_HI 倍及以上得满分。
    """
    mult = min(
        tx_count_m5 / max(C.MIN_TX_M5, 1),
        volume_m5_sol / max(C.MIN_VOLUME_M5_SOL, 1e-9),
    )
    lo = C.ACTIVITY_MULT_LO
    if mult <= lo:
        return 0.0
    span = math.log(C.ACTIVITY_MULT_HI / lo)
    return min(1.0, math.log(mult / lo) / span)


# ★ 打分口径版本。任何改变分数**量纲**的改动（分项函数、权重、折扣）都必须 +1。
#
# 不带版本号的后果已经发生过一次：activity_s 从饱和线性改成对数刻度（e7d36a7）
# 之后，历史分 39.9~75.7 与新口径的可达上限 56.77 混在同一列里，没有任何标记能
# 把两批分开——任何「多少分以上该买」的阈值都是在两把不同的尺子上平均出来的。
# 版本号 + 分项 + 权重一起落盘，才能按口径分组、在组内校准。
SCORING_VERSION = 3

MOMENTUM_WEIGHTS: dict[str, float] = {
    "rebound": 30,
    "buy_sell": 25,
    "activity": 20,
    "near_high": 15,
    "streak": 10,
}
DIP_WEIGHTS: dict[str, float] = {
    "ath_drop": 40,
    "panic": 25,
    "whale": 20,
    "activity": 15,
}
# 无真实序列折扣：仅当既无 OHLCV 也无可用自采序列时才 ×0.8。
# 有任一真数据源则满分量纲；要调需连门槛一起调。
NO_OHLCV_MULT = 0.8
# 未过硬门槛的候选只用于看板排序，降权后与开仓分不是一个量纲
NOT_PASS_MULT = 0.35


def _momentum_parts(c: Candidate) -> dict[str, float]:
    """动量分项 0~1：回升甜点(偏 20~40) + 买压 + 活跃 + 贴近高点 + 连涨。"""
    # 甜点取「严格门槛」中点附近，延伸段略降权但仍可高分
    sweet = (C.REBOUND_MIN + C.REBOUND_STRICT_FROM) / 2.0
    half = max(1e-6, (C.REBOUND_STRICT_FROM - C.REBOUND_MIN) / 2.0)
    rebound_s = max(0.0, 1.0 - abs(c.rebound - sweet) / half)
    if c.rebound > C.REBOUND_STRICT_FROM:
        rebound_s = max(rebound_s, 0.55)  # 延伸加速不归零
    return {
        "rebound": rebound_s,
        "buy_sell": min(1.0, max(0.0, (c.buy_sell_ratio - C.BUY_SELL_RATIO_MIN) / 2.0)),
        "activity": activity_score(c.tx_count_m5, c.volume_m5_sol),
        "near_high": max(0.0, 1.0 - c.pullback / max(C.PULLBACK_MAX, 1e-6)),
        "streak": min(1.0, c.price_streak / max(C.MOMENTUM_STREAK_MIN + 2, 1)),
    }


def _dip_parts(c: Candidate) -> dict[str, float]:
    mid_drop = (C.ATH_DROP_MIN + C.ATH_DROP_MAX) / 2.0
    half = max(1e-6, (C.ATH_DROP_MAX - C.ATH_DROP_MIN) / 2.0)
    return {
        "ath_drop": max(0.0, 1.0 - abs(c.ath_drop - mid_drop) / half),
        "panic": min(1.0, max(0.0, (c.panic_ratio - C.PANIC_RATIO_MIN) / 3.0)),
        "whale": min(1.0, max(0.0, (c.whale_dump_pct - C.WHALE_DUMP_MIN) / 0.25)),
        "activity": min(
            1.0,
            max(
                0.0,
                min(
                    c.tx_count_m5 / max(C.MIN_TX_M5 * 4, 1),
                    c.volume_m5_sol / max(C.MIN_VOLUME_M5_SOL * 4, 1e-9),
                ),
            ),
        ),
    }


def score_breakdown(c: Candidate) -> dict[str, Any]:
    """分数 + 分项 + 权重 + 版本。score == round(Σ weights[k]*parts[k] * mult, 2)。"""
    if C.IS_MOMENTUM:
        mode = "momentum"
        parts = _momentum_parts(c)
        weights = MOMENTUM_WEIGHTS
        mult = 1.0 if (c.ohlcv_ok or c.self_hist_usable) else NO_OHLCV_MULT
    else:
        mode = "dip"
        parts = _dip_parts(c)
        weights = DIP_WEIGHTS
        mult = 1.0
    raw = sum(weights[k] * parts[k] for k in weights)
    return {
        "ver": SCORING_VERSION,
        "mode": mode,
        "score": round(raw * mult, 2),
        "parts": {k: round(v, 4) for k, v in parts.items()},
        "weights": dict(weights),
        "mult": mult,
    }


def score_momentum(c: Candidate) -> float:
    parts = _momentum_parts(c)
    raw = sum(MOMENTUM_WEIGHTS[k] * parts[k] for k in MOMENTUM_WEIGHTS)
    return round(
        raw * (1.0 if (c.ohlcv_ok or c.self_hist_usable) else NO_OHLCV_MULT), 2
    )


def score_dip(c: Candidate) -> float:
    parts = _dip_parts(c)
    return round(sum(DIP_WEIGHTS[k] * parts[k] for k in DIP_WEIGHTS), 2)


def score_candidate(c: Candidate) -> float:
    return float(score_breakdown(c)["score"])


def _reason_key(reason: str) -> str:
    if "延伸段" in reason:
        return "延伸加严"
    for prefix, key in (
        ("上线时长", "时间窗"),
        ("回升", "动量回升"),
        ("近5m涨幅", "5m转正"),
        ("近5m买盘", "买盘推升"),
        ("连续上涨", "连续上涨"),
        ("买/卖比", "买卖比"),
        ("ATH跌幅", "ATH区间"),
        ("恐慌比", "恐慌比"),
        ("单户清仓", "鲸抛集中度"),
        ("近5m成交额", "5m成交额"),
        ("近5m成交", "5m成交笔数"),
        ("流动性", "流动性"),
        ("高位回撤", "回撤红线"),
        ("砸盘残废", "砸盘否决"),
        ("价格无效", "价格无效"),
    ):
        if reason.startswith(prefix):
            return key
    return reason[:24]


def clamp_drawdown_pct(price: float, peak: float) -> float:
    """看板回撤%：(price-peak)/peak ∈ [-100, 0]。peak 自动抬到 ≥ price。"""
    px = float(price or 0)
    pk = max(float(peak or 0), px)
    if pk <= 0 or px <= 0:
        return 0.0
    return round(max(-100.0, min(0.0, (px / pk - 1.0) * 100.0)), 2)


def apply_price_drawdown(row: dict[str, Any], price: float) -> None:
    """就地更新 ath_price / drawdown_pct / pullback_pct / ath_drop_pct。"""
    px = float(price or 0)
    ath = max(float(row.get("ath_price") or 0), px)
    row["ath_price"] = ath
    dd = clamp_drawdown_pct(px, ath)
    row["drawdown_pct"] = dd
    row["pullback_pct"] = dd
    row["ath_drop_pct"] = round(-dd, 2)  # 正数跌幅，兼容旧字段


def _norm_symbol(sym: str | None) -> str:
    return "".join(ch for ch in (sym or "").upper() if ch.isalnum())


def _is_clone_symbol(sym: str | None) -> bool:
    """借用 BTC/SOL 等知名名的仿盘（精确名或名+短后缀/数字）。"""
    n = _norm_symbol(sym)
    if not n:
        return False
    blocked = C.CLONE_SYMBOL_BLOCKLIST
    if n in blocked:
        return True
    # BTC2 / SOL69 / ETHX — 品牌名 + ≤3 位短后缀
    for brand in blocked:
        if len(brand) < 3:
            continue
        if n.startswith(brand) and len(n) <= len(brand) + 3:
            suffix = n[len(brand) :]
            if not suffix or suffix.isdigit() or (len(suffix) <= 2 and suffix.isalpha()):
                return True
    return False


def sanitize_candidates(raw: list[Candidate]) -> list[Candidate]:
    """按 mint 去重 + 同名 Symbol 只留最优 + 拦截知名名仿盘。

    主键永远是 mint；同 Symbol 多 mint 时保留流动性/成交额最高者，其余淘汰。
    """
    by_mint: dict[str, Candidate] = {}
    for c in raw:
        mint = (c.mint or "").strip()
        if not mint:
            continue
        # 峰值自愈：ath 不得低于现价
        if c.price > 0 and (c.ath_price or 0) < c.price:
            c.ath_price = c.price
        prev = by_mint.get(mint)
        if prev is None:
            by_mint[mint] = c
            continue
        # 同 mint 重复行：留更新的/流动性更高的
        if (c.liquidity_sol, c.volume_m5_sol, c.tx_count_m5) > (
            prev.liquidity_sol,
            prev.volume_m5_sol,
            prev.tx_count_m5,
        ):
            by_mint[mint] = c

    # 仿盘名拦截
    cleaned: list[Candidate] = []
    for c in by_mint.values():
        if _is_clone_symbol(c.symbol):
            logger.info("仿盘名拦截 symbol=%s mint=%s…", c.symbol, c.mint[:8])
            continue
        cleaned.append(c)

    # 同 Symbol 多 mint：只留综合最优，防止看板同名混淆
    best_by_sym: dict[str, Candidate] = {}
    for c in cleaned:
        key = _norm_symbol(c.symbol) or c.mint
        cur = best_by_sym.get(key)
        if cur is None:
            best_by_sym[key] = c
            continue
        score_new = (c.liquidity_sol, c.volume_m5_sol, c.tx_count_m5, -c.age_minutes)
        score_old = (cur.liquidity_sol, cur.volume_m5_sol, cur.tx_count_m5, -cur.age_minutes)
        if score_new > score_old:
            logger.info(
                "同名 Symbol 去重保留 mint=%s… 淘汰 mint=%s… symbol=%s",
                c.mint[:8],
                cur.mint[:8],
                c.symbol,
            )
            best_by_sym[key] = c
        else:
            logger.info(
                "同名 Symbol 去重保留 mint=%s… 淘汰 mint=%s… symbol=%s",
                cur.mint[:8],
                c.mint[:8],
                c.symbol,
            )

    return list(best_by_sym.values())


def filter_candidates(raw: list[Candidate]) -> list[dict[str, Any]]:
    """返回通过 + 接近条件的候选（供 UI）；开仓侧只吃 hard_pass=True。

    看板不堆垃圾：零流动性 / 零成交 / 行情过旧 的拒绝项直接不展示。
    """
    raw = sanitize_candidates(raw)
    out: list[dict[str, Any]] = []
    hidden_trash = 0
    for c in raw:
        track, fails = classify_track(c)
        ok = track is not None
        c.track = track
        # 垃圾盘不进看板
        if not ok:
            stale = False
            if c.data_ts > 0 and (time.time() - c.data_ts) > float(C.SIGNAL_MAX_AGE_SEC):
                stale = True
            liq_floor = min(float(C.TRACK_A_LIQ_MIN), float(C.LIQUIDITY_MIN_SOL))
            vol_floor = min(float(C.TRACK_A_MIN_VOL_M5), float(C.MIN_VOLUME_M5_SOL))
            trash = (
                stale
                or c.liquidity_sol < liq_floor
                or c.volume_m5_sol < vol_floor
                or c.liquidity_sol <= 0
                or c.volume_m5_sol <= 0
            )
            if trash:
                hidden_trash += 1
                continue
        row = c.to_row()
        row["hard_pass"] = ok
        row["track"] = track
        row["fail_reasons"] = fails
        breakdown = score_breakdown(c)
        if not ok:
            breakdown["score"] = round(float(breakdown["score"]) * NOT_PASS_MULT, 2)
            breakdown["mult"] = round(float(breakdown["mult"]) * NOT_PASS_MULT, 4)
        row["score"] = breakdown["score"]
        # 随候选一路带到成交记录：开仓时的打分口径必须跟分数存在一起
        row["scoring"] = breakdown
        # 最低分门槛：过线但分太低（如 Found 39）仍禁止开仓
        if ok and float(row["score"]) < float(C.ENTRY_MIN_SCORE):
            row["hard_pass"] = False
            row["fail_reasons"] = list(fails) + [
                f"评分 {row['score']:.1f} < {C.ENTRY_MIN_SCORE:.0f}（质量不足）"
            ]
        out.append(row)
    # 排序键（可解释优先级，不再单纯靠分数当胜率旋钮）：
    #   1) 能开仓的排前  2) 有真实K线的排前（验证过的边）  3) 才是分数
    out.sort(
        key=lambda x: (
            1 if x["hard_pass"] else 0,
            1 if x.get("ohlcv_ok") else 0,
            x["score"],
        ),
        reverse=True,
    )
    reasons = Counter(
        _reason_key(reason)
        for row in out
        if not row["hard_pass"]
        for reason in row["fail_reasons"]
    )
    logger.info(
        "FILTER mode=%s 总数=%d 过线=%d(A=%d,B=%d) 拒绝展示=%d 隐藏垃圾=%d 主要原因=%s",
        C.STRATEGY_MODE,
        len(out),
        sum(1 for row in out if row["hard_pass"]),
        sum(1 for row in out if row.get("track") == "A"),
        sum(1 for row in out if row.get("track") == "B"),
        sum(1 for row in out if not row["hard_pass"]),
        hidden_trash,
        ", ".join(f"{k}:{v}" for k, v in reasons.most_common(4)) or "无",
    )
    return out


def _pseudo_mint(seed: str) -> str:
    h = hashlib.sha256(seed.encode()).hexdigest()
    return h[:32] + "pump"


def generate_demo_universe(n: int = 24) -> list[Candidate]:
    """演示宇宙：混入少量满足当前策略模式的合格标的。"""
    now = time.time()
    rng = random.Random(int(now // 30))
    names = [
        "PEPE2", "WIFX", "BONKAI", "MOONDOG", "CATWIF", "SOLPIG",
        "RUGLESS", "DEGENX", "FROGAI", "CHADSOL", "MEMEKING", "PUMPKING",
        "LIQTRAP", "SCAV", "DUMPSTER", "REKTIN", "WHALESLAY", "PANICBUY",
        "ATHFALL", "SPREADZ", "GHOSTSOL", "NIGHTCAP", "TOMBSTONE", "AFTERMATH",
    ]
    cands: list[Candidate] = []
    for i in range(n):
        sym = names[i % len(names)] + str(rng.randint(1, 99))
        mint = _pseudo_mint(f"{sym}-{int(now // 60)}")
        force_hit = i % 4 == 0
        age_m = (
            rng.uniform(C.AGE_MIN_MINUTES + 2, C.AGE_MAX_MINUTES - 5)
            if force_hit
            else rng.uniform(2, 200)
        )
        listed_at = now - age_m * 60
        ath = 10 ** rng.uniform(-8, -3)

        if C.IS_MOMENTUM:
            if force_hit:
                rebound = rng.uniform(C.REBOUND_MIN + 0.01, C.REBOUND_MAX - 0.01)
                pullback = rng.uniform(0.02, C.PULLBACK_MAX - 0.01)
                bs_ratio = rng.uniform(C.BUY_SELL_RATIO_MIN + 0.1, 3.0)
                chg5 = rng.uniform(2.0, 18.0)
                streak = C.MOMENTUM_STREAK_MIN + rng.randint(0, 3)
                liq = rng.uniform(C.LIQUIDITY_MIN_SOL + 2, 150)
                tx = rng.randint(C.MIN_TX_M5, C.MIN_TX_M5 * 4)
                vol = rng.uniform(C.MIN_VOLUME_M5_SOL, C.MIN_VOLUME_M5_SOL * 5)
            else:
                rebound = rng.uniform(0.05, 0.8)
                pullback = rng.uniform(0.05, 0.6)
                bs_ratio = rng.uniform(0.4, 2.5)
                chg5 = rng.uniform(-20.0, 25.0)
                streak = rng.randint(0, 4)
                liq = rng.uniform(2, 80)
                tx = rng.randint(0, 40)
                vol = rng.uniform(0, 20)
            price = ath * (1.0 - pullback)
            jitter = 1.0 + math.sin(now / 7.0 + i) * 0.008
            price *= jitter
            sells_m5 = max(1, int(rng.uniform(5, 40)))
            buys_m5 = max(1, int(sells_m5 * bs_ratio))
            chg15 = rebound * 100.0
            chg30 = rebound * 100.0 * rng.uniform(0.85, 1.05)
            cands.append(
                Candidate(
                    mint=mint,
                    symbol=sym,
                    listed_at=listed_at,
                    ath_price=ath,
                    price=price,
                    buy_vol=float(buys_m5),
                    sell_vol=float(sells_m5),
                    whale_dump_pct=0.0,
                    liquidity_sol=liq,
                    tx_count_m5=buys_m5 + sells_m5 if force_hit else tx,
                    volume_m5_sol=vol,
                    volume_m5_usd=vol * 140,
                    buys_m5=buys_m5,
                    sells_m5=sells_m5,
                    chg_m5=chg5,
                    chg_m15=chg15,
                    chg_m30=chg30,
                    # 模拟盘自带「自采序列」：让 rebound 有可信来源，
                    # 否则纸面/影子模式会因缺历史而全军覆没。
                    self_low=price / (1.0 + rebound),
                    self_high=ath,
                    self_span_min=float(C.PX_HIST_WINDOW_MIN),
                    self_points=int(C.REBOUND_SELF_MIN_POINTS) * 3,
                    self_px_15m_ago=price / (1.0 + rebound),
                    price_streak=streak,
                )
            )
        else:
            if force_hit:
                drop = rng.uniform(C.ATH_DROP_MIN + 0.02, C.ATH_DROP_MAX - 0.02)
                panic = rng.uniform(C.PANIC_RATIO_MIN + 0.2, 6.0)
                whale = rng.uniform(C.WHALE_DUMP_MIN + 0.02, 0.98)
            else:
                drop = rng.uniform(0.2, 0.92)
                panic = rng.uniform(0.5, 4.0)
                whale = rng.uniform(0.1, 0.9)
            price = ath * (1.0 - drop)
            jitter = 1.0 + math.sin(now / 7.0 + i) * 0.008
            price *= jitter
            buy = rng.uniform(5, 80)
            sell = buy * panic
            cands.append(
                Candidate(
                    mint=mint,
                    symbol=sym,
                    listed_at=listed_at,
                    ath_price=ath,
                    price=price,
                    buy_vol=buy,
                    sell_vol=sell,
                    whale_dump_pct=whale,
                    liquidity_sol=rng.uniform(8, 120),
                    tx_count_m5=rng.randint(5, 80) if force_hit else rng.randint(0, 30),
                    volume_m5_sol=rng.uniform(1.5, 30) if force_hit else rng.uniform(0, 8),
                    volume_m5_usd=rng.uniform(120, 3000) if force_hit else rng.uniform(0, 800),
                )
            )
    return cands


def scan_market() -> list[dict[str, Any]]:
    """扫描入口。

    - DEMO_SCAN=1：演示宇宙（纸面验证策略过滤/出场）
    - DEMO_SCAN=0（实盘/影子）：GeckoTerminal 真实新池观察 + 严苛过滤；
      行情失败时返回空列表（宁可空仓，绝不用假币下真单）
    """
    if C.DEMO_SCAN:
        return filter_candidates(generate_demo_universe())
    from .market_data import OHLCV_MAX_POOLS_PER_SCAN, enrich_ohlcv, scan_live

    cands = scan_live()
    # 只对「看起来有动量」的少数候选拉真实 K 线，避免 Gecko 限流。
    # 名额有限，按动量排序后再截断，别把配额浪费在字典顺序靠前的死盘上。
    promising = sorted(
        (
            c
            for c in cands
            if c.pool and c.chg_m5 > 0 and c.chg_m15 > 0 and c.volume_m5_sol > 0
        ),
        key=lambda c: c.chg_m5,
        reverse=True,
    )[:OHLCV_MAX_POOLS_PER_SCAN]
    try:
        enrich_ohlcv(promising)
    except Exception:
        logger.exception("OHLCV 富集失败（继续用反推回升）")
    return filter_candidates(cands)
