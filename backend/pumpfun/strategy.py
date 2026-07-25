"""超跌清算严苛过滤引擎。"""

from __future__ import annotations

import hashlib
import math
import random
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from . import config as C


@dataclass
class Candidate:
    mint: str
    symbol: str
    listed_at: float  # unix ts
    ath_price: float
    price: float
    buy_vol: float
    sell_vol: float
    whale_dump_pct: float  # 0~1
    bid: float
    ask: float
    liquidity_sol: float = 0.0

    @property
    def age_minutes(self) -> float:
        return max(0.0, (time.time() - self.listed_at) / 60.0)

    @property
    def ath_drop(self) -> float:
        if self.ath_price <= 0:
            return 0.0
        return 1.0 - (self.price / self.ath_price)

    @property
    def panic_ratio(self) -> float:
        if self.buy_vol <= 0:
            return 999.0 if self.sell_vol > 0 else 0.0
        return self.sell_vol / self.buy_vol

    @property
    def spread(self) -> float:
        mid = (self.bid + self.ask) / 2.0 if (self.bid + self.ask) > 0 else self.price
        if mid <= 0:
            return 0.0
        return abs(self.ask - self.bid) / mid

    def to_row(self) -> dict[str, Any]:
        return {
            "mint": self.mint,
            "symbol": self.symbol,
            "listed_at": datetime.fromtimestamp(self.listed_at, tz=timezone.utc).isoformat(),
            "age_minutes": round(self.age_minutes, 1),
            "ath_drop_pct": round(self.ath_drop * 100, 2),
            "panic_ratio": round(self.panic_ratio, 2),
            "whale_dump_pct": round(self.whale_dump_pct * 100, 1),
            "spread_pct": round(self.spread * 100, 2),
            "price": self.price,
            "ath_price": self.ath_price,
            "liquidity_sol": round(self.liquidity_sol, 3),
        }


def pass_hard_filters(c: Candidate) -> tuple[bool, list[str]]:
    """严苛过滤：全部通过才可进入捡尸候选。"""
    fails: list[str] = []
    if not (C.AGE_MIN_MINUTES <= c.age_minutes <= C.AGE_MAX_MINUTES):
        fails.append(
            f"上线时长 {c.age_minutes:.0f}m ∉ [{C.AGE_MIN_MINUTES:.0f},{C.AGE_MAX_MINUTES:.0f}]"
        )
    if c.ath_drop < C.ATH_DROP_MIN:
        fails.append(f"ATH跌幅 {c.ath_drop*100:.1f}% < {C.ATH_DROP_MIN*100:.0f}%")
    if c.panic_ratio < C.PANIC_RATIO_MIN:
        fails.append(f"恐慌比 {c.panic_ratio:.2f} < {C.PANIC_RATIO_MIN}")
    if c.whale_dump_pct < C.WHALE_DUMP_MIN:
        fails.append(f"单户清仓 {c.whale_dump_pct*100:.0f}% < {C.WHALE_DUMP_MIN*100:.0f}%")
    if c.spread <= C.SPREAD_MIN:
        fails.append(f"价差 {c.spread*100:.2f}% ≤ {C.SPREAD_MIN*100:.0f}%")
    if c.price <= 0 or c.ath_price <= 0:
        fails.append("价格无效")
    return (len(fails) == 0, fails)


def score_candidate(c: Candidate) -> float:
    """综合捡尸分：跌幅 + 恐慌 + 鲸抛 + 价差（越高越优先）。"""
    drop_s = min(1.0, max(0.0, (c.ath_drop - C.ATH_DROP_MIN) / 0.15))
    panic_s = min(1.0, max(0.0, (c.panic_ratio - C.PANIC_RATIO_MIN) / 3.0))
    whale_s = min(1.0, max(0.0, (c.whale_dump_pct - C.WHALE_DUMP_MIN) / 0.25))
    spread_s = min(1.0, max(0.0, (c.spread - C.SPREAD_MIN) / 0.08))
    return round(40 * drop_s + 25 * panic_s + 20 * whale_s + 15 * spread_s, 2)


def filter_candidates(raw: list[Candidate]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for c in raw:
        ok, fails = pass_hard_filters(c)
        row = c.to_row()
        row["hard_pass"] = ok
        row["fail_reasons"] = fails
        row["score"] = score_candidate(c) if ok else 0.0
        if ok:
            out.append(row)
    out.sort(key=lambda x: x["score"], reverse=True)
    return out


def _pseudo_mint(seed: str) -> str:
    h = hashlib.sha256(seed.encode()).hexdigest()
    return h[:32] + "pump"


def generate_demo_universe(n: int = 24) -> list[Candidate]:
    """演示用扫描宇宙：混入少量满足严苛条件的超跌标的。"""
    now = time.time()
    rng = random.Random(int(now // 30))  # 每 30s 换一批特征，价格帧内抖动
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
        # 约 1/4 做成「合格捡尸」形态
        force_hit = i % 4 == 0
        age_m = rng.uniform(C.AGE_MIN_MINUTES + 5, C.AGE_MAX_MINUTES - 10) if force_hit else rng.uniform(5, 500)
        listed_at = now - age_m * 60
        ath = 10 ** rng.uniform(-8, -3)
        if force_hit:
            drop = rng.uniform(C.ATH_DROP_MIN + 0.02, 0.95)
            panic = rng.uniform(C.PANIC_RATIO_MIN + 0.2, 6.0)
            whale = rng.uniform(C.WHALE_DUMP_MIN + 0.02, 0.98)
            spread = rng.uniform(C.SPREAD_MIN + 0.005, 0.12)
        else:
            drop = rng.uniform(0.2, 0.92)
            panic = rng.uniform(0.5, 4.0)
            whale = rng.uniform(0.1, 0.9)
            spread = rng.uniform(0.005, 0.1)
        price = ath * (1.0 - drop)
        # 帧内微抖动
        jitter = 1.0 + math.sin(now / 7.0 + i) * 0.008
        price *= jitter
        mid = price
        half = mid * spread / 2.0
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
                bid=mid - half,
                ask=mid + half,
                liquidity_sol=rng.uniform(8, 120),
            )
        )
    return cands


def scan_market() -> list[dict[str, Any]]:
    """扫描入口：当前以演示宇宙为主，可替换为真实 Pump.fun / Birdeye 源。"""
    if C.DEMO_SCAN:
        return filter_candidates(generate_demo_universe())
    # 预留：真实源接入点
    return filter_candidates(generate_demo_universe())
