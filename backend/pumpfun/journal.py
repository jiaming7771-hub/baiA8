"""Pump.fun 交易日志与 24h 滚动统计。"""

from __future__ import annotations

import csv
import io
import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import config as C

logger = logging.getLogger("pumpfun.journal")
_lock = threading.Lock()

# 不含阈值的标签：改配置也不会让它们失真，可以直接常量化。
ACTION_LABELS = {
    "buy": "买入",
    "trail_stop": "移动止盈清仓",
    "be_stop": "保本止损清仓",
    "time_stop": "时间止损",
    "early_fade": "早期闷亏早砍",
    "dead_stop": "死盘早砍",
    "whale_dump": "早期大户砸盘熔断",
    "rent_block": "租金/底仓拦截",
    "duplicate_buy_block": "重复买入拦截",
    "liquidity_collapse": "流动性坍塌",
    "liquidity_escape": "抽池强制逃生",
    "stale_mark": "标价冻结强制离场",
    "manual_flatten": "手动清仓",
    "write_off": "无流动性核销",
    "safety_block": "链上安全拦截",
    "holder_block": "筹码集中度拦截",
    "roundtrip_block": "往返流动性拦截",
    "blacklist_block": "恶名钱包黑名单",
    "route_failover": "毕业迁移路由切换",
    "swap_error": "链上交易异常",
}

EXIT_REASONS = {
    "buy": "",
    "early_fade": "早期闷亏早砍（从未真正浮盈且已明显变红，先砍小亏）",
    "dead_stop": "死盘早砍（开仓初期无动量/成交枯竭）",
    "whale_dump": "早期大户/老鼠仓净流出熔断（不等硬止损）",
    "liquidity_escape": "盘口假涨/抽池：可兑现远低于成本，全仓强制 salvage",
    "stale_mark": "链上价长时间读不到，标价已冻结 — 不再盲飞，强制 salvage",
    "manual_flatten": "手动全平",
}

# ★ 含阈值的标签只能由「记录自带的阈值」渲染，不能在渲染时读当下配置。
#
# 阈值是会调的：同一份历史里 hard_stop 出现过 -13% / -25% / -35%，TP1 出现过
# +22% / +25%。若拿当下配置去渲染，复盘时会看到「-22% 才止损」而实际那笔是
# -13% 就砍了——结论会跟着错。而且全局 C.TP1_PCT 跟真正触发的轨道阈值
# （TRACK_A/B_TP1）本来就不是一个数（当前 0.40 vs 0.25），即使不改配置，
# 拿全局值当标签也是错的。
_LABEL_FMT = {
    "tp1": "第一止盈(+{pct:g}%)",
    "hard_stop": "价格硬止损(-{pct:g}%)",
}
_REASON_FMT = {
    "tp1": "达到第一止盈+{pct:g}%（卖出{sell:g}%）",
    "hard_stop": "价格硬止损（浮亏≤-{pct:g}%，立刻全仓斩仓）",
    "trail_stop": "回撤止盈（峰值回落≥{pct:g}%）",
    "be_stop": "保本接管清仓（时间豁免后回落至保本价/峰值回落≥{pct:g}%）",
    "time_stop": "时间止损（持仓≥{pct:g}分钟且未盈利）",
}
# 老记录没存阈值：宁可不写数字，也不能补一个当下配置的数字冒充历史
_LABEL_NO_THRESHOLD = {
    "tp1": "第一止盈",
    "hard_stop": "价格硬止损",
}
_REASON_NO_THRESHOLD = {
    "tp1": "达到第一止盈（阈值未记录）",
    "hard_stop": "价格硬止损（阈值未记录）",
    "trail_stop": "回撤止盈（峰值回落，阈值未记录）",
    "be_stop": "保本接管清仓（回落至保本价/峰值回落，阈值未记录）",
    "time_stop": "时间止损（持仓超时且未盈利，阈值未记录）",
}


# time_stop 的阈值单位是分钟，不是比例，不能乘 100
_MINUTE_THRESHOLD_ACTIONS = frozenset({"time_stop"})


def _pct(value: Any) -> float | None:
    """比例（0.22）→ 展示用百分点（22）。None / 非数 → None。"""
    if value is None:
        return None
    try:
        return round(float(value) * 100.0, 2)
    except (TypeError, ValueError):
        return None


def _display_threshold(action: str, value: Any) -> float | None:
    if action in _MINUTE_THRESHOLD_ACTIONS:
        try:
            return None if value is None else round(float(value), 2)
        except (TypeError, ValueError):
            return None
    return _pct(value)


def action_label(
    action: str,
    *,
    threshold_pct: float | None = None,
    sell_ratio: float | None = None,
) -> str:
    """动作中文标签。含阈值的动作必须传入当时生效的阈值。"""
    if action in _LABEL_FMT:
        pct = _display_threshold(action, threshold_pct)
        if pct is None:
            return _LABEL_NO_THRESHOLD[action]
        return _LABEL_FMT[action].format(pct=pct, sell=_pct(sell_ratio) or 0)
    return ACTION_LABELS.get(action, action)


def exit_reason_text(
    action: str,
    *,
    threshold_pct: float | None = None,
    sell_ratio: float | None = None,
) -> str:
    if action in _REASON_FMT:
        pct = _display_threshold(action, threshold_pct)
        if pct is None:
            return _REASON_NO_THRESHOLD[action]
        return _REASON_FMT[action].format(pct=pct, sell=_pct(sell_ratio) or 0)
    return EXIT_REASONS.get(action, "")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(dt: datetime | None = None) -> str:
    return (dt or _utc_now()).isoformat()


def _parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    s = str(value).strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def ensure_dirs() -> None:
    C.DATA_DIR.mkdir(parents=True, exist_ok=True)
    C.TRADING_LOGS_DIR.mkdir(parents=True, exist_ok=True)


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    ensure_dirs()
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_execution_log(row: dict[str, Any]) -> None:
    """写入 trading_logs/bot_execution.log 详尽结构化行。"""
    ensure_dirs()
    line = json.dumps(row, ensure_ascii=False)
    with C.EXEC_LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(f"{_utc_iso()} {line}\n")
    logger.info(
        "EXEC %s %s %s pnl=%s reason=%s",
        row.get("action"),
        row.get("symbol"),
        row.get("mint", "")[:8],
        row.get("pnl_sol"),
        row.get("exit_reason") or "-",
    )


def record_trade(
    *,
    action: str,
    mint: str,
    symbol: str,
    amount_sol: float,
    price: float,
    pnl_sol: float | None = None,
    pnl_percent: float | None = None,
    exit_reason: str | None = None,
    dry_run: bool = True,
    shadow: bool = False,
    metrics: dict[str, Any] | None = None,
    position_id: str | None = None,
    threshold_pct: float | None = None,
    sell_ratio: float | None = None,
    basis_price: float | None = None,
) -> dict[str, Any]:
    """记录一笔结构化交易到 daily_trades.jsonl + 执行日志。

    threshold_pct / sell_ratio 是**这笔当时真正生效**的阈值，随记录一起落盘，
    标签由它渲染。basis_price 是判定用的基准价（mark 口径），跟 price 一起存，
    复盘才能解释「为什么 -97% 还记成第一止盈」这类基差问题。
    """
    metrics = metrics or {}
    ts = _utc_iso()
    label = action_label(action, threshold_pct=threshold_pct, sell_ratio=sell_ratio)
    reason = (
        exit_reason
        if exit_reason is not None
        else exit_reason_text(action, threshold_pct=threshold_pct, sell_ratio=sell_ratio)
    )
    row: dict[str, Any] = {
        "timestamp": ts,
        "mint": mint,
        "symbol": symbol,
        "action": action,
        "action_label": label,
        "threshold_pct": None if threshold_pct is None else float(threshold_pct),
        "sell_ratio": None if sell_ratio is None else float(sell_ratio),
        "basis_price": None if basis_price is None else float(basis_price),
        "amount_sol": round(float(amount_sol), 8),
        "price": float(price),
        "pnl_sol": None if pnl_sol is None else round(float(pnl_sol), 8),
        "pnl_percent": None if pnl_percent is None else round(float(pnl_percent), 4),
        "exit_reason": reason,
        "dry_run": bool(dry_run),
        "shadow": bool(shadow),
        "position_id": position_id,
        # 关键指标（便于复盘）
        "panic_ratio": metrics.get("panic_ratio"),
        "ath_drop_pct": metrics.get("ath_drop_pct"),
        "whale_dump_pct": metrics.get("whale_dump_pct"),
        "tx_count_m5": metrics.get("tx_count_m5"),
        "volume_m5_sol": metrics.get("volume_m5_sol"),
        "volume_m5_usd": metrics.get("volume_m5_usd"),
        "slippage_pct": metrics.get("slippage_pct"),
        "score": metrics.get("score"),
        "age_minutes": metrics.get("age_minutes"),
        "max_float_pnl_pct": metrics.get("max_float_pnl_pct"),
    }
    with _lock:
        _append_jsonl(C.DAILY_TRADES_FILE, row)
        # 兼容旧 trades.jsonl
        _append_jsonl(C.TRADES_FILE, {"event": action, **row})
        write_execution_log(row)
    return row


def record_alert(
    *,
    action: str,
    message: str,
    mint: str = "",
    symbol: str = "",
    amount_sol: float = 0.0,
    context: dict[str, Any] | None = None,
    dry_run: bool = False,
    shadow: bool = False,
) -> dict[str, Any]:
    """高危告警写入 trades.jsonl（租金拦截 / 路由切换 / 链上异常）。"""
    ctx = dict(context or {})
    ctx["alert"] = True
    ctx["message"] = message
    return record_trade(
        action=action,
        mint=mint or "—",
        symbol=symbol or action,
        amount_sol=amount_sol,
        price=0.0,
        pnl_sol=None,
        pnl_percent=None,
        exit_reason=message,
        dry_run=dry_run,
        shadow=shadow,
        metrics=ctx,
    )


def _fill_labels(row: dict[str, Any]) -> None:
    """补全缺失的标签，就地改字典（只补内存里的读出结果，不回写历史文件）。

    优先级：记录自带的 threshold_pct → 记录写入时冻结的 action_label →
    不带数字的兜底。任何情况下都不拿当下配置去补一个数字。
    """
    action = str(row.get("action") or "")
    if not action:
        return
    threshold = row.get("threshold_pct")
    if threshold is not None:
        row["action_label"] = action_label(
            action, threshold_pct=threshold, sell_ratio=row.get("sell_ratio")
        )
        if not row.get("exit_reason"):
            row["exit_reason"] = exit_reason_text(
                action, threshold_pct=threshold, sell_ratio=row.get("sell_ratio")
            )
        return
    if not row.get("action_label"):
        row["action_label"] = action_label(action)
    if row.get("exit_reason") is None:
        row["exit_reason"] = exit_reason_text(action)


def load_trades(*, hours: float = 24.0, limit: int | None = None) -> list[dict[str, Any]]:
    ensure_dirs()
    cutoff = _utc_now() - timedelta(hours=hours)
    rows: list[dict[str, Any]] = []
    path = C.DAILY_TRADES_FILE
    if not path.exists():
        # 回退读旧文件
        path = C.TRADES_FILE
    if not path.exists():
        return []
    with _lock:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = _parse_ts(row.get("timestamp") or row.get("ts"))
                if ts is None or ts < cutoff:
                    continue
                # 规范化旧格式
                if "action" not in row and row.get("event"):
                    ev = row["event"]
                    if ev == "open":
                        row["action"] = "buy"
                        row["amount_sol"] = row.get("sol_spent") or row.get("amount_sol")
                        row["price"] = row.get("entry") or row.get("price")
                    elif ev == "close_partial":
                        reason = row.get("reason") or "tp1"
                        row["action"] = reason
                        row["amount_sol"] = row.get("amount_sol")
                        if row.get("amount_sol") is None and row.get("qty") and row.get("price"):
                            row["amount_sol"] = float(row["qty"]) * float(row["price"])
                _fill_labels(row)
                rows.append(row)
    rows.sort(key=lambda r: str(r.get("timestamp") or ""), reverse=True)
    if limit is not None:
        rows = rows[:limit]
    return rows


def compute_stats_24h(
    bankroll: float | None = None,
    *,
    equity: float | None = None,
    realized_pnl: float | None = None,
    unrealized_pnl: float | None = None,
    dry_run: bool | None = None,
) -> dict[str, Any]:
    """24 小时滚动：交易次数、胜率；总盈亏优先用资产净值法。

    总盈亏口径（与右下角权益增量对齐）：
      total_pnl = equity - bankroll
                = realized_pnl + unrealized_pnl   （若传入）
    不再简单加总分批止盈腿上的 pnl_sol（会漏计开仓摩擦、与账户脱节）。

    dry_run:
      - True  → 只统计纸面成交
      - False → 只统计实盘成交（隔离纸面历史）
      - None  → 全部
    """
    trades = load_trades(hours=24.0)
    if dry_run is True:
        trades = [t for t in trades if bool(t.get("dry_run", True))]
    elif dry_run is False:
        trades = [t for t in trades if not bool(t.get("dry_run", True))]

    bankroll = float(bankroll if bankroll is not None else C.BANKROLL_SOL)

    buys = [t for t in trades if t.get("action") == "buy"]
    exits = [
        t
        for t in trades
        if t.get("action")
        in (
            "tp1",
            "trail_stop",
            "be_stop",
            "time_stop",
            "hard_stop",
            "early_fade",
            "dead_stop",
            "whale_dump",
            "write_off",
        )
    ]
    closed_legs = [t for t in exits if t.get("pnl_sol") is not None]
    wins = [t for t in closed_legs if float(t.get("pnl_sol") or 0) > 0]
    # 流水加总仅作对照口径，看板主数字用净值法
    legs_pnl = sum(float(t.get("pnl_sol") or 0) for t in closed_legs)
    win_rate = (len(wins) / len(closed_legs) * 100.0) if closed_legs else 0.0

    if equity is not None:
        total_pnl = float(equity) - bankroll
        method = "nav_equity"
    elif realized_pnl is not None:
        total_pnl = float(realized_pnl) + float(unrealized_pnl or 0.0)
        method = "realized_plus_unrealized"
    else:
        # 无账户快照时退化：流水加总（不推荐用于大屏主数字）
        total_pnl = legs_pnl
        method = "legs_sum_fallback"

    pnl_pct_bankroll = (total_pnl / bankroll * 100.0) if bankroll > 0 else 0.0

    return {
        "window_hours": 24,
        "total_trades": len(buys),
        "total_actions": len(trades),
        "exit_count": len(closed_legs),
        "win_count": len(wins),
        "loss_count": max(0, len(closed_legs) - len(wins)),
        "win_rate": round(win_rate, 1),
        "total_pnl_sol": round(total_pnl, 4),
        "total_pnl_pct": round(pnl_pct_bankroll, 2),
        "legs_pnl_sol": round(legs_pnl, 6),  # 对照：分批腿加总（可能高于净值）
        "pnl_method": method,
        "bankroll_sol": bankroll,
        "equity_sol": None if equity is None else round(float(equity), 4),
        "realized_pnl_sol": None if realized_pnl is None else round(float(realized_pnl), 4),
        "unrealized_pnl_sol": None if unrealized_pnl is None else round(float(unrealized_pnl), 4),
        "dry_run_filter": dry_run,
        "updated_at": _utc_iso(),
    }


def lifetime_realized_pnl() -> float:
    """全历史已实现盈亏（SOL）；账户文件缺失时用于重建余额。"""
    total = 0.0
    for path in (C.DAILY_TRADES_FILE, C.TRADES_FILE):
        if not path.exists():
            continue
        with _lock:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if row.get("pnl_sol") is None:
                        continue
                    try:
                        total += float(row["pnl_sol"])
                    except (TypeError, ValueError):
                        continue
        break  # 两个文件互为镜像，只统计其中一个
    return total


def clear_trades() -> dict[str, Any]:
    """清空结构化交易记录（保留执行日志文件）。"""
    ensure_dirs()
    with _lock:
        for path in (C.DAILY_TRADES_FILE, C.TRADES_FILE):
            if path.exists():
                path.write_text("", encoding="utf-8")
    return {"ok": True, "cleared": True, "stats": compute_stats_24h(), "trades": []}


def trades_to_csv(hours: float = 24.0) -> str:
    rows = load_trades(hours=hours)
    buf = io.StringIO()
    fields = [
        "timestamp",
        "symbol",
        "mint",
        "action",
        "action_label",
        "amount_sol",
        "price",
        "pnl_sol",
        "pnl_percent",
        "exit_reason",
        "dry_run",
        "panic_ratio",
        "ath_drop_pct",
        "whale_dump_pct",
        "tx_count_m5",
        "volume_m5_sol",
        "volume_m5_usd",
        "slippage_pct",
    ]
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for r in reversed(rows):  # 时间正序导出
        writer.writerow({k: r.get(k) for k in fields})
    return buf.getvalue()
