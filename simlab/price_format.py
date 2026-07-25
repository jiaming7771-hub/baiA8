"""动态价格精度：按量级保留有效数字，避免山寨/Meme 微单价被截断成相同显示。"""

from __future__ import annotations

import math


def price_decimals(price: float, *, significant: int = 6) -> int:
    """根据价格量级返回应保留的小数位数（默认约 6 位有效数字）。"""
    p = abs(float(price))
    if not math.isfinite(p) or p == 0:
        return 2
    if p >= 1000:
        return 2
    if p >= 100:
        return 3
    if p >= 1:
        return 4
    if p >= 0.1:
        return 5
    if p >= 0.01:
        return 6
    # < 0.01：按有效数字推算小数位；极小价至少保证能区分 0.00000345 vs 0.00000340
    exp = math.floor(math.log10(p))
    decimals = int(significant) - exp - 1
    if p < 0.0001:
        return max(8, min(decimals, 18))
    return max(6, min(decimals, 16))


def round_price(price: float, *, significant: int = 6) -> float:
    """API/落库展示用舍入；计算路径应使用原始 float，勿在此之前截断。"""
    p = float(price)
    if not math.isfinite(p):
        return p
    return round(p, price_decimals(p, significant=significant))
