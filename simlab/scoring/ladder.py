"""分批挂单阶梯与硬止损重构（防倒挂 + 抗插针安全边际）。

原始点位来自 `calculate_advanced_trading_levels`（纯函数，禁止改动）。
该模块作为其下游安全层，负责把「单点 entry/stop」展开为可执行阶梯，并强制：

    现价 > 第一仓(30%) > 第二仓(70%) > 硬止损

同时保证 stop_loss < 均价 entry < take_profit，且止损与第二仓之间留出
1.5%~2.5% 的呼吸空间，避免瞬时插针误扫。
"""

from __future__ import annotations

from typing import Any

from simlab.scoring import weights as W


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _down_pct(ref: float, target: float) -> float:
    """target 位于 ref 下方的百分比。"""
    if ref <= 0:
        return 0.0
    return (ref - target) / ref * 100.0


def build_safe_trade_plan(
    price: float,
    entry_raw: float,
    stop_raw: float,
    take_raw: float,
    *,
    atr: float | None = None,
) -> dict[str, Any]:
    """把原始点位重构为防倒挂的阶梯计划。

    返回 dict 始终包含 `valid` 与 `violations`；`valid=False` 时上层应拒绝挂单。
    """
    violations: list[str] = []
    p = float(price or 0)
    e_raw = float(entry_raw or 0)

    if p <= 0 or e_raw <= 0:
        return {"valid": False, "violations": ["现价或原始入场价无效"]}

    atr_abs = float(atr) if atr else 0.0
    if atr_abs <= 0:
        # ATR 缺失时用两仓最小间距反推一个保守替代值
        atr_abs = p * W.T2_GAP_MIN_PCT

    # ---- ① 第一仓：贴支撑，但必须低于现价一个最小安全距离 ----
    t1 = min(e_raw, p * (1.0 - W.T1_MIN_GAP_PCT))
    # 过远的第一仓没有埋伏意义，向上收敛到最大回撤边界
    t1 = max(t1, p * (1.0 - W.T1_MAX_GAP_PCT))

    if t1 >= p:
        return {"valid": False, "violations": ["第一仓不低于现价，拒绝挂单"]}

    # ---- ② 第二仓：严格低于第一仓，间距由 ATR 决定并夹在 min~max ----
    gap = _clamp(
        W.T2_GAP_ATR_MULT * atr_abs,
        t1 * W.T2_GAP_MIN_PCT,
        t1 * W.T2_GAP_MAX_PCT,
    )
    t2 = t1 - gap
    if t2 <= 0:
        return {"valid": False, "violations": ["第二仓价格计算为非正数"]}

    # ---- ③ 硬止损：第二仓下方 1.5%~2.5%（ATR 在区间内取值）----
    buffer = _clamp(
        W.STOP_BUFFER_ATR_MULT * atr_abs,
        t2 * W.STOP_BUFFER_MIN_PCT,
        t2 * W.STOP_BUFFER_MAX_PCT,
    )
    stop = t2 - buffer

    # 均价：按 30/70 权重加权，作为盈亏比与风控口径的唯一 entry
    avg_entry = W.TRANCHE1_RATIO * t1 + W.TRANCHE2_RATIO * t2

    # 总风险硬顶：止损不得深于均价的 MAX_TOTAL_RISK_PCT
    min_allowed_stop = avg_entry * (1.0 - W.MAX_TOTAL_RISK_PCT)
    if stop < min_allowed_stop:
        stop = min_allowed_stop

    # 收紧后仍需保证止损低于第二仓至少 STOP_BUFFER_MIN_PCT
    hard_ceiling = t2 * (1.0 - W.STOP_BUFFER_MIN_PCT)
    if stop > hard_ceiling:
        stop = hard_ceiling

    if stop <= 0 or stop >= t2:
        return {"valid": False, "violations": ["止损价无法满足第二仓下方安全边际"]}

    # ---- ④ 止盈：目标盈亏比优先，ATR/百分比作为上限，但不得压穿 RR 底线 ----
    risk_pct = (avg_entry - stop) / avg_entry
    raw_tp_pct = (float(take_raw) / avg_entry - 1.0) if take_raw else 0.0
    target_pct = max(W.TP_MIN_PCT, W.TP_RR_TARGET * risk_pct, raw_tp_pct)

    cap_pct = min(W.TP_MAX_PCT, W.TP_ATR_MULT_CAP * atr_abs / avg_entry)
    tp_pct = min(target_pct, cap_pct)

    # 止损被刻意拉宽后，ATR 上限可能把 RR 压到不可交易水平：以 RR 底线为准
    min_rr_pct = W.HARD_RR_MIN * risk_pct
    if tp_pct < min_rr_pct:
        tp_pct = min(min_rr_pct, W.TP_MAX_PCT)
    tp_pct = max(tp_pct, W.TP_MIN_PCT)
    take = avg_entry * (1.0 + tp_pct)

    # ---- ⑤ 防倒挂总校验 ----
    if not (stop < t2 < t1 < p):
        violations.append("阶梯顺序倒挂：需满足 现价 > 第一仓 > 第二仓 > 止损")
    if not (stop < avg_entry < take):
        violations.append("stop_loss < entry < take_profit 不成立")
    stop_gap_pct = _down_pct(t2, stop)
    if stop_gap_pct < W.STOP_BUFFER_MIN_PCT * 100 - 1e-6:
        violations.append(f"止损距第二仓仅 {stop_gap_pct:.2f}%，低于安全边际")

    risk = avg_entry - stop
    rr = (take - avg_entry) / risk if risk > 0 else None

    return {
        "valid": not violations,
        "violations": violations,
        # 统一口径的三个核心点位
        "entry": avg_entry,
        "stop_loss": stop,
        "take_profit": take,
        # 阶梯明细
        "tranche_1_price": t1,
        "tranche_2_price": t2,
        "tranche_gap_pct": round(_down_pct(t1, t2), 4),
        "stop_gap_pct": round(stop_gap_pct, 4),
        # 展示口径
        "distance_pct": round(_down_pct(p, avg_entry), 4),
        "first_entry_distance_pct": round(_down_pct(p, t1), 4),
        "risk_distance": round(risk / avg_entry, 6),
        "risk_reward_ratio": None if rr is None else round(rr, 4),
        "atr_used": atr_abs,
    }


def build_batch_suggestion(plan: dict[str, Any]) -> dict[str, Any]:
    """由安全计划生成分批挂单建议（30% 试错 + 70% 补仓）。"""
    if not plan or not plan.get("tranche_1_price"):
        return {}
    t1 = float(plan["tranche_1_price"])
    t2 = float(plan["tranche_2_price"])
    return {
        "tranche_1": {
            "label": "第一仓·轻仓试错",
            "ratio": W.TRANCHE1_RATIO,
            "price": t1,
            "note": f"贴支撑第一道防线，仓位 {int(W.TRANCHE1_RATIO * 100)}%",
        },
        "tranche_2": {
            "label": "第二仓·下探补仓",
            "ratio": W.TRANCHE2_RATIO,
            "price": t2,
            "note": (
                f"较第一仓再低 {plan.get('tranche_gap_pct', 0):.2f}%，"
                f"仓位 {int(W.TRANCHE2_RATIO * 100)}%"
            ),
        },
        "hard_stop": {
            "label": "硬止损",
            "price": float(plan["stop_loss"]),
            "note": f"位于第二仓下方 {plan.get('stop_gap_pct', 0):.2f}%，抗插针",
        },
        "avg_entry": float(plan["entry"]),
        "style": "震荡企稳·轻仓埋伏",
        "ladder_valid": bool(plan.get("valid")),
    }
