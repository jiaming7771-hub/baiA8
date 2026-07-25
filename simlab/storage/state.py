"""持久化：组合状态 / 成交 / 事件。"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from simlab import config

_lock = threading.Lock()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_state() -> dict[str, Any]:
    path = config.STATE_PATH
    if not path.exists():
        return {
            "cash": config.INITIAL_EQUITY,
            "equity_start": config.INITIAL_EQUITY,
            "positions": {},
            "pending": {},
            "cycle": 0,
            "realized_pnl": 0.0,
            "fees_paid": 0.0,
            "wins": 0,
            "losses": 0,
            "updated_at": utc_now(),
        }
    with _lock:
        return json.loads(path.read_text(encoding="utf-8"))


def save_state(state: dict[str, Any]) -> None:
    state["updated_at"] = utc_now()
    with _lock:
        config.STATE_PATH.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    row = {**row, "ts": row.get("ts") or utc_now()}
    with _lock:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def append_trade(row: dict[str, Any]) -> None:
    append_jsonl(config.TRADES_PATH, row)


def append_event(row: dict[str, Any]) -> None:
    append_jsonl(config.EVENTS_PATH, row)


def append_text(path: Path, line: str) -> None:
    with _lock:
        with path.open("a", encoding="utf-8") as f:
            f.write(line.rstrip() + "\n")
