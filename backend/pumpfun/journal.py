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

ACTION_LABELS = {
    "buy": "买入",
    "tp1": "第一止盈",
    "trail_stop": "回撤清仓",
    "time_stop": "时间止损",
    "hard_stop": "硬止损",
}

EXIT_REASONS = {
    "buy": "",
    "tp1": "达到第一止盈+28%（卖出55%）",
    "trail_stop": "回撤止盈（峰值回撤13%）",
    "time_stop": "时间止损（持仓≥11分钟）",
    "hard_stop": "硬止损",
}


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
    metrics: dict[str, Any] | None = None,
    position_id: str | None = None,
) -> dict[str, Any]:
    """记录一笔结构化交易到 daily_trades.jsonl + 执行日志。"""
    metrics = metrics or {}
    ts = _utc_iso()
    action_label = ACTION_LABELS.get(action, action)
    reason = exit_reason if exit_reason is not None else EXIT_REASONS.get(action, "")
    row: dict[str, Any] = {
        "timestamp": ts,
        "mint": mint,
        "symbol": symbol,
        "action": action,
        "action_label": action_label,
        "amount_sol": round(float(amount_sol), 8),
        "price": float(price),
        "pnl_sol": None if pnl_sol is None else round(float(pnl_sol), 8),
        "pnl_percent": None if pnl_percent is None else round(float(pnl_percent), 4),
        "exit_reason": reason,
        "dry_run": bool(dry_run),
        "position_id": position_id,
        # 关键指标（便于复盘）
        "panic_ratio": metrics.get("panic_ratio"),
        "ath_drop_pct": metrics.get("ath_drop_pct"),
        "whale_dump_pct": metrics.get("whale_dump_pct"),
        "spread_pct": metrics.get("spread_pct"),
        "slippage_pct": metrics.get("slippage_pct"),
        "score": metrics.get("score"),
        "age_minutes": metrics.get("age_minutes"),
    }
    with _lock:
        _append_jsonl(C.DAILY_TRADES_FILE, row)
        # 兼容旧 trades.jsonl
        _append_jsonl(C.TRADES_FILE, {"event": action, **row})
        write_execution_log(row)
    return row


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
                        row["action_label"] = ACTION_LABELS["buy"]
                        row["amount_sol"] = row.get("sol_spent") or row.get("amount_sol")
                        row["price"] = row.get("entry") or row.get("price")
                    elif ev == "close_partial":
                        reason = row.get("reason") or "tp1"
                        row["action"] = reason
                        row["action_label"] = ACTION_LABELS.get(reason, reason)
                        row["exit_reason"] = EXIT_REASONS.get(reason, reason)
                        row["amount_sol"] = row.get("amount_sol")
                        if row.get("amount_sol") is None and row.get("qty") and row.get("price"):
                            row["amount_sol"] = float(row["qty"]) * float(row["price"])
                rows.append(row)
    rows.sort(key=lambda r: str(r.get("timestamp") or ""), reverse=True)
    if limit is not None:
        rows = rows[:limit]
    return rows


def compute_stats_24h(bankroll: float | None = None) -> dict[str, Any]:
    """24 小时滚动：交易次数、总盈亏、胜率。"""
    trades = load_trades(hours=24.0)
    bankroll = float(bankroll if bankroll is not None else C.BANKROLL_SOL)

    # 总交易次数：买入 + 各类出场都计一次动作；看板「交易次数」以完整回合（买入）为主，另附动作数
    buys = [t for t in trades if t.get("action") == "buy"]
    exits = [t for t in trades if t.get("action") in ("tp1", "trail_stop", "time_stop", "hard_stop")]
    # 胜率按有 pnl 的出场腿统计（含部分止盈）
    closed_legs = [t for t in exits if t.get("pnl_sol") is not None]
    wins = [t for t in closed_legs if float(t.get("pnl_sol") or 0) > 0]
    total_pnl = sum(float(t.get("pnl_sol") or 0) for t in closed_legs)
    win_rate = (len(wins) / len(closed_legs) * 100.0) if closed_legs else 0.0
    pnl_pct_bankroll = (total_pnl / bankroll * 100.0) if bankroll > 0 else 0.0

    return {
        "window_hours": 24,
        "total_trades": len(buys),  # 今日/24h 开仓次数
        "total_actions": len(trades),
        "exit_count": len(closed_legs),
        "win_count": len(wins),
        "loss_count": max(0, len(closed_legs) - len(wins)),
        "win_rate": round(win_rate, 1),
        "total_pnl_sol": round(total_pnl, 6),
        "total_pnl_pct": round(pnl_pct_bankroll, 2),
        "bankroll_sol": bankroll,
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
        "spread_pct",
        "slippage_pct",
    ]
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for r in reversed(rows):  # 时间正序导出
        writer.writerow({k: r.get(k) for k in fields})
    return buf.getvalue()
