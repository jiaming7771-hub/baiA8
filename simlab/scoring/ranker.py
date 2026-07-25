"""硬性过滤 + TOP10 / 推荐前三强排序主流程。"""

from __future__ import annotations

import logging
from typing import Any

from simlab.scoring import weights as W
from simlab.scoring.ladder import build_batch_suggestion, build_safe_trade_plan
from simlab.scoring.operability import (
    distance_pct,
    risk_distance,
    risk_reward_ratio,
)
from simlab.scoring.total_score import compute_total_score

logger = logging.getLogger("simlab.scoring.ranker")


def _vol_ratio_15m(volume_series) -> float | None:
    """近5根均量 / 近20根均量；用于判定是否极度缩量。"""
    try:
        import pandas as pd

        if volume_series is None:
            return None
        s = volume_series if hasattr(volume_series, "iloc") else pd.Series(volume_series)
        if len(s) < 20:
            return None
        recent = float(s.iloc[-5:].mean())
        base = float(s.iloc[-20:].mean())
        if base <= 0:
            return None
        return recent / base
    except Exception:
        return None


def check_hard_filters(
    *,
    price: float,
    entry: float,
    stop_loss: float,
    take_profit: float,
    volume_15m=None,
    vol_ratio: float | None = None,
) -> dict[str, Any]:
    """硬性门槛校验；返回是否通过及失败原因列表。"""
    reasons: list[str] = []
    dist = distance_pct(price, entry)
    risk = risk_distance(entry, stop_loss)
    rr = risk_reward_ratio(entry, stop_loss, take_profit)
    ratio = vol_ratio if vol_ratio is not None else _vol_ratio_15m(volume_15m)

    if dist is None or not (W.HARD_DIST_LO <= dist <= W.HARD_DIST_HI):
        reasons.append(
            f"距离不符({None if dist is None else round(dist, 2)}% ∉ "
            f"[{W.HARD_DIST_LO},{W.HARD_DIST_HI}])"
        )
    if risk is None or risk > W.HARD_RISK_MAX:
        reasons.append(
            f"风险距离过大({None if risk is None else round(risk * 100, 2)}% > "
            f"{W.HARD_RISK_MAX * 100}%)"
        )
    if rr is None or rr < W.HARD_RR_MIN:
        reasons.append(
            f"盈亏比不足({None if rr is None else round(rr, 2)} < {W.HARD_RR_MIN})"
        )
    if ratio is not None and ratio < W.HARD_VOL_RATIO_MIN:
        reasons.append(f"15m量能极度萎缩(ratio={ratio:.2f} < {W.HARD_VOL_RATIO_MIN})")
    # 量能缺失不直接否决（网络/字段缺失），仅标记
    elif ratio is None:
        pass

    return {
        "hard_pass": len(reasons) == 0,
        "hard_fail_reasons": reasons,
        "vol_ratio_15m": None if ratio is None else round(ratio, 4),
    }


def build_batch_orders(
    price: float,
    entry: float,
    stop_loss: float = 0.0,
    take_profit: float = 0.0,
    *,
    atr: float | None = None,
) -> dict[str, Any]:
    """轻仓埋伏分批挂单（防倒挂）：30% 贴支撑试错 + 70% 下探补仓。

    保留旧签名以兼容外部调用；内部统一走 `build_safe_trade_plan` 安全层。
    """
    plan = build_safe_trade_plan(
        price, entry, stop_loss or entry * 0.98, take_profit or entry * 1.01, atr=atr
    )
    return build_batch_suggestion(plan)


def enrich_candidate(coin: dict[str, Any]) -> dict[str, Any]:
    """为单个候选币种计算总分、硬过滤、分批挂单。

    期望字段：
      symbol, price, quote_volume, vs_btc（或 vs_btc_24h / vs_btc_1h）,
      funding_rate, levels{entry,stop_loss,take_profit,atr},
      可选 volume_15m / vol_ratio_15m
    """
    row = dict(coin)
    price = float(row.get("price") or 0)
    levels = row.get("levels") or {}
    entry = float(levels.get("entry") or row.get("order_entry") or 0)
    stop = float(levels.get("stop_loss") or row.get("order_stop") or 0)
    take = float(levels.get("take_profit") or row.get("order_take") or 0)
    atr = levels.get("atr", row.get("order_atr"))

    vs = row.get("vs_btc")
    if vs is None:
        vs = row.get("vs_btc_24h", row.get("vs_btc_1h", 0))

    if price <= 0 or entry <= 0 or stop <= 0 or take <= 0:
        row["hard_pass"] = False
        row["hard_fail_reasons"] = ["点位或价格无效"]
        row["total_score"] = 0.0
        row["score_detail"] = {}
        return row

    # 安全层：重建阶梯与硬止损，后续评分/过滤一律使用修正后的点位
    plan = build_safe_trade_plan(
        price, entry, stop, take, atr=None if atr is None else float(atr)
    )
    if not plan.get("valid"):
        row["hard_pass"] = False
        row["hard_fail_reasons"] = plan.get("violations") or ["阶梯校验失败"]
        row["total_score"] = 0.0
        row["score_detail"] = {}
        row["ladder_violations"] = plan.get("violations") or []
        return row

    entry = float(plan["entry"])
    stop = float(plan["stop_loss"])
    take = float(plan["take_profit"])

    scored = compute_total_score(
        quote_volume=float(row.get("quote_volume") or 0),
        vs_btc_pct=float(vs or 0),
        funding_rate=row.get("funding_rate"),
        price=price,
        entry=entry,
        stop_loss=stop,
        take_profit=take,
        atr=None if atr is None else float(atr),
    )
    hard = check_hard_filters(
        price=price,
        entry=entry,
        stop_loss=stop,
        take_profit=take,
        volume_15m=row.get("volume_15m"),
        vol_ratio=row.get("vol_ratio_15m"),
    )

    op = scored["operability"]
    row.update(
        {
            "total_score": scored["total_score"],
            "score_volume": scored["score_volume"],
            "score_rel_strength": scored["score_rel_strength"],
            "score_funding": scored["score_funding"],
            "score_volatility": scored["score_volatility"],
            "score_operability": scored["score_operability"],
            "score_detail": scored,
            "atr_pct": scored.get("atr_pct"),
            "distance_pct": op.get("distance_pct"),
            "risk_distance": op.get("risk_distance"),
            "risk_reward_ratio": op.get("risk_reward_ratio"),
            "hard_pass": hard["hard_pass"],
            "hard_fail_reasons": hard["hard_fail_reasons"],
            "vol_ratio_15m": hard.get("vol_ratio_15m"),
            "entry": entry,
            "stop_loss": stop,
            "take_profit": take,
            "raw_levels": levels,
            "tranche_1_price": plan.get("tranche_1_price"),
            "tranche_2_price": plan.get("tranche_2_price"),
            "tranche_gap_pct": plan.get("tranche_gap_pct"),
            "stop_gap_pct": plan.get("stop_gap_pct"),
            "first_entry_distance_pct": plan.get("first_entry_distance_pct"),
            "ladder_valid": plan.get("valid"),
            "batch_orders": build_batch_suggestion(plan),
        }
    )
    return row


def rank_ambush_rotation(
    candidates: list[dict[str, Any]],
    *,
    top_n: int = W.TOP_N,
    top_k: int = W.TOP_K,
) -> dict[str, Any]:
    """山寨轮动排序主函数（可运行示例的核心入口）。

    流程：
      1) 为每个候选计算五维总分 + 硬性过滤
      2) 按 total_score 排出完整 TOP10（保留是否通过硬过滤标注）
      3) 从 hard_pass 集合中取前三强；不足时用未通过者降级补足并标记 fallback
    """
    enriched = [enrich_candidate(c) for c in candidates]
    enriched.sort(key=lambda x: float(x.get("total_score") or 0), reverse=True)

    top10 = []
    for i, c in enumerate(enriched[:top_n], 1):
        item = dict(c)
        item["rank"] = i
        top10.append(item)

    passed = [c for c in enriched if c.get("hard_pass")]
    fallback_used = False
    if len(passed) >= top_k:
        top3_src = passed[:top_k]
    else:
        # 数量不足：先用通过者，再按总分从全体补足
        fallback_used = True
        pool = passed + [c for c in enriched if not c.get("hard_pass")]
        top3_src = pool[:top_k]

    top3 = []
    for i, c in enumerate(top3_src, 1):
        item = {
            "rank": i,
            "symbol": c.get("symbol"),
            "pair": c.get("pair"),
            "price": c.get("price"),
            "total_score": c.get("total_score"),
            "hard_pass": c.get("hard_pass"),
            "hard_fail_reasons": c.get("hard_fail_reasons") or [],
            "is_fallback": (not c.get("hard_pass")),
            "score_volume": c.get("score_volume"),
            "score_rel_strength": c.get("score_rel_strength"),
            "score_funding": c.get("score_funding"),
            "score_volatility": c.get("score_volatility"),
            "score_operability": c.get("score_operability"),
            "entry": c.get("entry"),
            "stop_loss": c.get("stop_loss"),
            "take_profit": c.get("take_profit"),
            "distance_pct": c.get("distance_pct"),
            "first_entry_distance_pct": c.get("first_entry_distance_pct"),
            "risk_distance": c.get("risk_distance"),
            "risk_reward_ratio": c.get("risk_reward_ratio"),
            "atr_pct": c.get("atr_pct"),
            "tranche_1_price": c.get("tranche_1_price"),
            "tranche_2_price": c.get("tranche_2_price"),
            "tranche_gap_pct": c.get("tranche_gap_pct"),
            "stop_gap_pct": c.get("stop_gap_pct"),
            "ladder_valid": c.get("ladder_valid"),
            "batch_orders": c.get("batch_orders"),
            "quote_volume": c.get("quote_volume"),
            "vs_btc_1h": c.get("vs_btc_1h"),
            "vs_btc_24h": c.get("vs_btc_24h"),
            "funding_rate": c.get("funding_rate"),
        }
        top3.append(item)

    logger.info(
        "rank top10=%s top3=%s fallback=%s",
        [f"{x['symbol']}:{x.get('total_score')}" for x in top10],
        [f"{x['symbol']}:{x.get('total_score')}" for x in top3],
        fallback_used,
    )
    return {
        "top10": top10,
        "top3": top3,
        "top3_fallback": fallback_used,
        "passed_count": len(passed),
        "candidate_count": len(enriched),
    }
