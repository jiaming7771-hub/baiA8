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
    price_streak: int = 0  # 最近扫描连续上涨次数

    @property
    def age_minutes(self) -> float:
        return max(0.0, (time.time() - self.listed_at) / 60.0)

    @property
    def ath_drop(self) -> float:
        if self.ath_price <= 0:
            return 0.0
        return 1.0 - (self.price / self.ath_price)

    @property
    def pullback(self) -> float:
        """距短期高点回撤（0=在高点，0.15=回撤15%）。"""
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
    def rebound(self) -> float:
        """从近 15/30 分钟窗口起点回升幅度（取更大者，小数）。

        Gecko 无真实 low：用正涨幅窗口反推 window_start ≈ price/(1+chg)，
        回升 = chg/100。要求落在 REBOUND_MIN~MAX。
        """
        best = 0.0
        for chg in (self.chg_m15, self.chg_m30):
            if chg > 0:
                best = max(best, chg / 100.0)
        return best

    def to_row(self) -> dict[str, Any]:
        return {
            "mint": self.mint,
            "symbol": self.symbol,
            "listed_at": datetime.fromtimestamp(self.listed_at, tz=timezone.utc).isoformat(),
            "age_minutes": round(self.age_minutes, 1),
            "ath_drop_pct": round(self.ath_drop * 100, 2),
            "pullback_pct": round(self.pullback * 100, 2),
            "rebound_pct": round(self.rebound * 100, 2),
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
            "ath_price": self.ath_price,
            "liquidity_sol": round(self.liquidity_sol, 3),
            "pool": self.pool,
            "dex": self.dex,
            "strategy_mode": C.STRATEGY_MODE,
        }


def _age_violent_exempt(c: Candidate, *, age_m: float, bs: float, vol_m5: float) -> bool:
    """老盘暴力豁免：超龄但 5m 巨量 + 极强买压 → 允许破例。"""
    if age_m <= C.AGE_MAX_MINUTES:
        return False
    return (
        vol_m5 >= C.AGE_EXEMPT_VOLUME_M5_SOL
        and c.tx_count_m5 >= C.AGE_EXEMPT_TX_M5
        and bs >= round(C.AGE_EXEMPT_BUY_SELL_RATIO, 2)
    )


def pass_momentum_filters(c: Candidate) -> tuple[bool, list[str]]:
    """顺势接力（优化版）：强动量放行 + 回撤红线绝对否决插针残局。"""
    fails: list[str] = []
    age_m = round(c.age_minutes, 1)
    rebound_pct = round(c.rebound * 100, 1)
    pullback_pct = round(c.pullback * 100, 1)
    bs = round(c.buy_sell_ratio, 2)
    vol_m5 = round(c.volume_m5_sol, 3)
    liq = round(c.liquidity_sol, 1)
    chg5 = round(c.chg_m5, 2)

    # ① 回撤红线（绝对核心，无任何豁免）：距短期高点 >15% = 插针/残局，一律拒
    if pullback_pct > round(C.PULLBACK_MAX * 100, 1):
        fails.append(
            f"高位回撤 {pullback_pct:.1f}% > {C.PULLBACK_MAX*100:.0f}%（插针/残局禁入）"
        )

    # ② 生命周期：8~120m；超龄仅「巨量二次拉盘」可豁免
    if age_m < C.AGE_MIN_MINUTES:
        fails.append(
            f"上线时长 {age_m:.0f}m < {C.AGE_MIN_MINUTES:.0f}m（过新）"
        )
    elif age_m > C.AGE_MAX_MINUTES:
        if _age_violent_exempt(c, age_m=age_m, bs=bs, vol_m5=vol_m5):
            # 破例放行，不记失败
            pass
        else:
            fails.append(
                f"上线时长 {age_m:.0f}m > {C.AGE_MAX_MINUTES:.0f}m"
                f"（需5m≥{C.AGE_EXEMPT_TX_M5}笔/"
                f"{C.AGE_EXEMPT_VOLUME_M5_SOL:.0f}SOL且买/卖≥{C.AGE_EXEMPT_BUY_SELL_RATIO}）"
            )

    # ③ 右侧回升 +20%~+40%
    if rebound_pct < round(C.REBOUND_MIN * 100, 1):
        fails.append(
            f"回升 {rebound_pct:.1f}% < {C.REBOUND_MIN*100:.0f}%（动量不足）"
        )
    if rebound_pct > round(C.REBOUND_MAX * 100, 1):
        fails.append(
            f"回升 {rebound_pct:.1f}% > {C.REBOUND_MAX*100:.0f}%（已延伸过远）"
        )

    # ④ 5m 转正 + 买盘推升 + 扫描连续上涨
    if chg5 <= 0:
        fails.append(f"近5m涨幅 {chg5:.2f}% ≤ 0（K线未转正）")
    if c.buys_m5 < c.sells_m5:
        fails.append(
            f"近5m买盘 {c.buys_m5} < 卖盘 {c.sells_m5}（未持续推高）"
        )
    if c.price_streak < C.MOMENTUM_STREAK_MIN:
        fails.append(
            f"连续上涨 {c.price_streak} < {C.MOMENTUM_STREAK_MIN} 次扫描"
        )

    # ⑤ 买/卖比与基础活跃（老盘豁免仍要满足基础买压下限）
    if bs < round(C.BUY_SELL_RATIO_MIN, 2):
        fails.append(f"买/卖比 {bs:.2f} < {C.BUY_SELL_RATIO_MIN}")

    if c.tx_count_m5 < C.MIN_TX_M5:
        fails.append(f"近5m成交 {c.tx_count_m5} 笔 < {C.MIN_TX_M5}（活跃不足）")
    if vol_m5 < C.MIN_VOLUME_M5_SOL:
        fails.append(
            f"近5m成交额 {vol_m5:.2f} SOL < {C.MIN_VOLUME_M5_SOL:.1f}（成交枯竭）"
        )
    if liq < C.LIQUIDITY_MIN_SOL:
        fails.append(f"流动性 {liq:.1f} SOL < {C.LIQUIDITY_MIN_SOL:.0f}（盘口枯竭）")

    if c.price <= 0 or c.ath_price <= 0:
        fails.append("价格无效")
    return (len(fails) == 0, fails)


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


def pass_hard_filters(c: Candidate) -> tuple[bool, list[str]]:
    if C.IS_MOMENTUM:
        return pass_momentum_filters(c)
    return pass_dip_filters(c)


def score_momentum(c: Candidate) -> float:
    """动量分：回升居中 + 买压 + 活跃 + 贴近高点。"""
    mid = (C.REBOUND_MIN + C.REBOUND_MAX) / 2.0
    half = max(1e-6, (C.REBOUND_MAX - C.REBOUND_MIN) / 2.0)
    rebound_s = max(0.0, 1.0 - abs(c.rebound - mid) / half)
    bs_s = min(1.0, max(0.0, (c.buy_sell_ratio - C.BUY_SELL_RATIO_MIN) / 2.0))
    activity_s = min(
        1.0,
        max(
            0.0,
            min(
                c.tx_count_m5 / max(C.MIN_TX_M5 * 3, 1),
                c.volume_m5_sol / max(C.MIN_VOLUME_M5_SOL * 3, 1e-9),
            ),
        ),
    )
    # 越贴近高点越好（回撤越小）
    near_high_s = max(0.0, 1.0 - c.pullback / max(C.PULLBACK_MAX, 1e-6))
    streak_s = min(1.0, c.price_streak / max(C.MOMENTUM_STREAK_MIN + 2, 1))
    return round(
        30 * rebound_s + 25 * bs_s + 20 * activity_s + 15 * near_high_s + 10 * streak_s,
        2,
    )


def score_dip(c: Candidate) -> float:
    mid_drop = (C.ATH_DROP_MIN + C.ATH_DROP_MAX) / 2.0
    half = max(1e-6, (C.ATH_DROP_MAX - C.ATH_DROP_MIN) / 2.0)
    drop_s = max(0.0, 1.0 - abs(c.ath_drop - mid_drop) / half)
    panic_s = min(1.0, max(0.0, (c.panic_ratio - C.PANIC_RATIO_MIN) / 3.0))
    whale_s = min(1.0, max(0.0, (c.whale_dump_pct - C.WHALE_DUMP_MIN) / 0.25))
    activity_s = min(
        1.0,
        max(
            0.0,
            min(
                c.tx_count_m5 / max(C.MIN_TX_M5 * 4, 1),
                c.volume_m5_sol / max(C.MIN_VOLUME_M5_SOL * 4, 1e-9),
            ),
        ),
    )
    return round(40 * drop_s + 25 * panic_s + 20 * whale_s + 15 * activity_s, 2)


def score_candidate(c: Candidate) -> float:
    if C.IS_MOMENTUM:
        return score_momentum(c)
    return score_dip(c)


def _reason_key(reason: str) -> str:
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
        ("价格无效", "价格无效"),
    ):
        if reason.startswith(prefix):
            return key
    return reason[:24]


def filter_candidates(raw: list[Candidate]) -> list[dict[str, Any]]:
    """返回通过 + 接近条件的候选（供 UI）；开仓侧只吃 hard_pass=True。"""
    out: list[dict[str, Any]] = []
    for c in raw:
        ok, fails = pass_hard_filters(c)
        row = c.to_row()
        row["hard_pass"] = ok
        row["fail_reasons"] = fails
        row["score"] = score_candidate(c) if ok else round(score_candidate(c) * 0.35, 2)
        out.append(row)
    out.sort(key=lambda x: (1 if x["hard_pass"] else 0, x["score"]), reverse=True)
    reasons = Counter(
        _reason_key(reason)
        for row in out
        if not row["hard_pass"]
        for reason in row["fail_reasons"]
    )
    logger.info(
        "FILTER mode=%s 总数=%d 过线=%d 拒绝=%d 主要原因=%s",
        C.STRATEGY_MODE,
        len(out),
        sum(1 for row in out if row["hard_pass"]),
        sum(1 for row in out if not row["hard_pass"]),
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
    from .market_data import scan_live

    return filter_candidates(scan_live())
