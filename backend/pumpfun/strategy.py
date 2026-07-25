"""超跌反弹「黄金猎杀」过滤引擎。"""

from __future__ import annotations

import hashlib
import logging
import math
import random
import time
from collections import Counter
from dataclasses import asdict, dataclass
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
    buy_vol: float  # m15 买单数
    sell_vol: float  # m15 卖单数
    whale_dump_pct: float  # 0~1
    liquidity_sol: float = 0.0
    tx_count_m5: int = 0
    volume_m5_sol: float = 0.0
    volume_m5_usd: float = 0.0

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

    def to_row(self) -> dict[str, Any]:
        return {
            "mint": self.mint,
            "symbol": self.symbol,
            "listed_at": datetime.fromtimestamp(self.listed_at, tz=timezone.utc).isoformat(),
            "age_minutes": round(self.age_minutes, 1),
            "ath_drop_pct": round(self.ath_drop * 100, 2),
            "panic_ratio": round(self.panic_ratio, 2),
            "whale_dump_pct": round(self.whale_dump_pct * 100, 1),
            "tx_count_m5": self.tx_count_m5,
            "volume_m5_sol": round(self.volume_m5_sol, 3),
            "volume_m5_usd": round(self.volume_m5_usd, 2),
            "price": self.price,
            "ath_price": self.ath_price,
            "liquidity_sol": round(self.liquidity_sol, 3),
        }


def pass_hard_filters(c: Candidate) -> tuple[bool, list[str]]:
    """黄金猎杀过滤：捕捉"超跌但仍活跃"的反弹盘，剔除归零死币。"""
    fails: list[str] = []
    # 时间窗口：避开开盘夹子期
    if not (C.AGE_MIN_MINUTES <= c.age_minutes <= C.AGE_MAX_MINUTES):
        fails.append(
            f"上线时长 {c.age_minutes:.0f}m ∉ [{C.AGE_MIN_MINUTES:.0f},{C.AGE_MAX_MINUTES:.0f}]"
        )
    # 超跌区间：太浅没肉
    if c.ath_drop < C.ATH_DROP_MIN:
        fails.append(f"ATH跌幅 {c.ath_drop*100:.1f}% < {C.ATH_DROP_MIN*100:.0f}%（超跌不足）")
    # 太深 = 接近归零死币，严禁抄
    if c.ath_drop > C.ATH_DROP_MAX:
        fails.append(f"ATH跌幅 {c.ath_drop*100:.1f}% > {C.ATH_DROP_MAX*100:.0f}%（疑似归零死币）")
    # 恐慌与鲸抛
    if c.panic_ratio < C.PANIC_RATIO_MIN:
        fails.append(f"恐慌比 {c.panic_ratio:.2f} < {C.PANIC_RATIO_MIN}")
    if c.whale_dump_pct < C.WHALE_DUMP_MIN:
        fails.append(f"单户清仓 {c.whale_dump_pct*100:.0f}% < {C.WHALE_DUMP_MIN*100:.0f}%")
    # 防归零①：近 5m 成交笔数、成交额均需活跃（买卖合计）
    if c.tx_count_m5 < C.MIN_TX_M5:
        fails.append(f"近5m成交 {c.tx_count_m5} 笔 < {C.MIN_TX_M5}（交易冻结）")
    if c.volume_m5_sol < C.MIN_VOLUME_M5_SOL:
        fails.append(
            f"近5m成交额 {c.volume_m5_sol:.2f} SOL < {C.MIN_VOLUME_M5_SOL:.1f}（成交枯竭）"
        )
    # 防归零②：池内流动性仍需充足
    if c.liquidity_sol < C.LIQUIDITY_MIN_SOL:
        fails.append(f"流动性 {c.liquidity_sol:.1f} SOL < {C.LIQUIDITY_MIN_SOL:.0f}（盘口枯竭）")
    if c.price <= 0 or c.ath_price <= 0:
        fails.append("价格无效")
    return (len(fails) == 0, fails)


def score_candidate(c: Candidate) -> float:
    """综合捡尸分：跌幅居中最优 + 恐慌 + 鲸抛 + 短时活跃度。"""
    # 跌幅甜点：区间中值附近（约 -60%）最优，越靠两端越低
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


def _reason_key(reason: str) -> str:
    for prefix, key in (
        ("上线时长", "时间窗"),
        ("ATH跌幅", "ATH区间"),
        ("恐慌比", "恐慌比"),
        ("单户清仓", "鲸抛集中度"),
        ("近5m成交额", "5m成交额"),
        ("近5m成交", "5m成交笔数"),
        ("流动性", "流动性"),
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
        "FILTER 总数=%d 过线=%d 拒绝=%d 主要原因=%s",
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
            drop = rng.uniform(C.ATH_DROP_MIN + 0.02, C.ATH_DROP_MAX - 0.02)
            panic = rng.uniform(C.PANIC_RATIO_MIN + 0.2, 6.0)
            whale = rng.uniform(C.WHALE_DUMP_MIN + 0.02, 0.98)
        else:
            drop = rng.uniform(0.2, 0.92)
            panic = rng.uniform(0.5, 4.0)
            whale = rng.uniform(0.1, 0.9)
        price = ath * (1.0 - drop)
        # 帧内微抖动
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
    - DEMO_SCAN=0（实盘）：GeckoTerminal 真实新池观察 + 严苛过滤；
      行情失败时返回空列表（宁可空仓，绝不用假币下真单）
    """
    if C.DEMO_SCAN:
        return filter_candidates(generate_demo_universe())
    from .market_data import scan_live

    return filter_candidates(scan_live())
