"""影子交易报告：记录每笔虚拟单的开仓/最高浮盈/平仓，并打印累计胜率。"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any

from . import config as C

logger = logging.getLogger("pumpfun.shadow")
_lock = threading.Lock()

# position_id -> open snapshot（含 peak 浮盈）
_open_book: dict[str, dict[str, Any]] = {}


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_closed() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not C.SHADOW_TRADES_FILE.exists():
        return rows
    try:
        for line in C.SHADOW_TRADES_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except Exception:
        logger.exception("读取影子成交失败")
    return rows


def _persist_summary(closed: list[dict[str, Any]]) -> dict[str, Any]:
    wins = [r for r in closed if float(r.get("pnl_sol") or 0) > 0]
    losses = [r for r in closed if float(r.get("pnl_sol") or 0) <= 0]
    total_pnl = sum(float(r.get("pnl_sol") or 0) for r in closed)
    n = len(closed)
    summary = {
        "updated_at": _utc(),
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / n, 4) if n else 0.0,
        "total_pnl_sol": round(total_pnl, 6),
        "avg_pnl_sol": round(total_pnl / n, 6) if n else 0.0,
        "avg_max_float_pct": round(
            sum(float(r.get("max_float_pnl_pct") or 0) for r in closed) / n, 2
        )
        if n
        else 0.0,
        "by_exit": {},
        "open_count": len(_open_book),
        "rules": {
            "hard_stop_pct": C.HARD_STOP_PCT,
            "tp1_pct": C.TP1_PCT,
            "tp1_sell": C.TP1_SELL_RATIO,
            "trail_dd": C.TRAIL_DRAWDOWN,
            "time_stop_m": C.TIME_STOP_MINUTES,
            "shadow_size_sol": C.SHADOW_SIZE_SOL,
        },
    }
    by_exit: dict[str, int] = {}
    for r in closed:
        k = str(r.get("exit_reason_code") or r.get("exit_reason") or "?")
        by_exit[k] = by_exit.get(k, 0) + 1
    summary["by_exit"] = by_exit
    try:
        C.TRADING_LOGS_DIR.mkdir(parents=True, exist_ok=True)
        C.SHADOW_SUMMARY_FILE.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        logger.exception("写影子汇总失败")
    return summary


def note_open(pos: dict[str, Any]) -> None:
    """影子开仓登记。"""
    pid = str(pos.get("id") or "")
    if not pid:
        return
    row = {
        "position_id": pid,
        "mint": pos.get("mint"),
        "symbol": pos.get("symbol"),
        "entry_price": float(pos.get("entry") or 0),
        "size_sol": float(pos.get("sol_spent") or C.SHADOW_SIZE_SOL),
        "opened_at": pos.get("opened_at_iso") or _utc(),
        "max_float_pnl_pct": 0.0,
        "max_float_pnl_sol": 0.0,
        "peak_price": float(pos.get("entry") or 0),
    }
    with _lock:
        _open_book[pid] = row
    logger.info(
        "👻 SHADOW OPEN %s @%.8g size=%.4f SOL（虚拟，无链上成交）",
        pos.get("symbol"),
        row["entry_price"],
        row["size_sol"],
    )


def note_mark(pos: dict[str, Any], price: float) -> None:
    """用真实盘口刷新最高浮盈。"""
    pid = str(pos.get("id") or "")
    entry = float(pos.get("entry") or 0)
    if not pid or entry <= 0 or price <= 0:
        return
    pnl_pct = (price - entry) / entry * 100.0
    size = float(pos.get("sol_spent") or C.SHADOW_SIZE_SOL)
    pnl_sol = size * (price - entry) / entry
    with _lock:
        book = _open_book.get(pid)
        if not book:
            return
        if pnl_pct > float(book.get("max_float_pnl_pct") or -1e18):
            book["max_float_pnl_pct"] = round(pnl_pct, 4)
            book["max_float_pnl_sol"] = round(pnl_sol, 8)
            book["peak_price"] = price
        # 同步回仓位，便于 snapshot
        pos["max_float_pnl_pct"] = book["max_float_pnl_pct"]
        pos["max_float_pnl_sol"] = book["max_float_pnl_sol"]


def note_partial_close(pos: dict[str, Any], *, reason: str, price: float, pnl_sol: float) -> None:
    """部分平仓（如 TP1）只更新浮盈峰值，最终报告等全平。"""
    note_mark(pos, price)
    logger.info(
        "👻 SHADOW PARTIAL %s reason=%s @%.8g pnl=%+.6f SOL max_float=%+.2f%%",
        pos.get("symbol"),
        reason,
        price,
        pnl_sol,
        float(pos.get("max_float_pnl_pct") or 0),
    )


def note_full_close(
    pos: dict[str, Any],
    *,
    reason: str,
    price: float,
    pnl_sol: float,
    pnl_percent: float,
) -> dict[str, Any]:
    """全平后写入报告并打印累计胜率。"""
    pid = str(pos.get("id") or "")
    note_mark(pos, price)
    with _lock:
        book = _open_book.pop(pid, {}) if pid else {}
        closed_row = {
            "closed_at": _utc(),
            "position_id": pid,
            "mint": pos.get("mint"),
            "symbol": pos.get("symbol"),
            "entry_price": float(pos.get("entry") or book.get("entry_price") or 0),
            "exit_price": float(price),
            "size_sol": float(pos.get("sol_spent") or book.get("size_sol") or C.SHADOW_SIZE_SOL),
            "max_float_pnl_pct": float(
                pos.get("max_float_pnl_pct")
                or book.get("max_float_pnl_pct")
                or 0
            ),
            "max_float_pnl_sol": float(
                pos.get("max_float_pnl_sol")
                or book.get("max_float_pnl_sol")
                or 0
            ),
            "pnl_sol": round(float(pnl_sol), 8),
            "pnl_percent": round(float(pnl_percent), 4),
            "exit_reason_code": reason,
            "exit_reason": reason,
            "opened_at": book.get("opened_at") or pos.get("opened_at_iso"),
            "age_minutes": round(
                (datetime.now(timezone.utc).timestamp() - float(pos.get("opened_at") or 0))
                / 60.0,
                2,
            )
            if pos.get("opened_at")
            else None,
            "shadow": True,
        }
        C.TRADING_LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with C.SHADOW_TRADES_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(closed_row, ensure_ascii=False) + "\n")
        all_closed = _load_closed()
        summary = _persist_summary(all_closed)

    _print_trade_report(closed_row, summary)
    return closed_row


def _print_trade_report(trade: dict[str, Any], summary: dict[str, Any]) -> None:
    n = int(summary.get("trades") or 0)
    logger.info("=" * 60)
    logger.info("📊 影子交易报告 #%d", n)
    logger.info("   币种     : %s", trade.get("symbol"))
    logger.info("   开仓价   : %.10g", float(trade.get("entry_price") or 0))
    logger.info("   平仓价   : %.10g", float(trade.get("exit_price") or 0))
    logger.info("   名义仓位 : %.4f SOL（虚拟）", float(trade.get("size_sol") or 0))
    logger.info(
        "   最高浮盈 : %+.2f%% (%+.6f SOL)",
        float(trade.get("max_float_pnl_pct") or 0),
        float(trade.get("max_float_pnl_sol") or 0),
    )
    logger.info(
        "   最终盈亏 : %+.6f SOL (%+.2f%%)",
        float(trade.get("pnl_sol") or 0),
        float(trade.get("pnl_percent") or 0),
    )
    logger.info("   出场原因 : %s | 持仓 %.1fm", trade.get("exit_reason_code"), trade.get("age_minutes") or 0)
    logger.info(
        "   累计统计 : 笔数=%d 胜=%d 负=%d 胜率=%.1f%% 净盈亏=%+.6f SOL",
        n,
        summary.get("wins"),
        summary.get("losses"),
        float(summary.get("win_rate") or 0) * 100,
        float(summary.get("total_pnl_sol") or 0),
    )
    logger.info(
        "   规则核对 : 硬止损-%.0f%% | TP1+%.0f%% | 移动回撤%.0f%% | 时间%.0fm",
        C.HARD_STOP_PCT * 100,
        C.TP1_PCT * 100,
        C.TRAIL_DRAWDOWN * 100,
        C.TIME_STOP_MINUTES,
    )
    logger.info("=" * 60)


def get_summary() -> dict[str, Any]:
    with _lock:
        closed = _load_closed()
        return _persist_summary(closed)


def print_summary() -> dict[str, Any]:
    summary = get_summary()
    logger.info(
        "📊 影子汇总 笔数=%d 胜率=%.1f%% 净盈亏=%+.6f SOL 在途=%d",
        summary.get("trades"),
        float(summary.get("win_rate") or 0) * 100,
        float(summary.get("total_pnl_sol") or 0),
        summary.get("open_count"),
    )
    return summary
