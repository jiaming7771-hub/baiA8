"""实盘状态持久化。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from simlab.live import config as C
from simlab.storage import state as store


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_live_state() -> dict[str, Any]:
    path = C.LIVE_STATE_PATH
    if not path.exists():
        return {
            "pending": {},
            "positions": {},
            "cycle": 0,
            "pool_used": 0.0,
            "updated_at": utc_now(),
        }
    return json.loads(path.read_text(encoding="utf-8"))


def save_live_state(state: dict[str, Any]) -> None:
    state["updated_at"] = utc_now()
    C.LIVE_STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def append_order_log(row: dict[str, Any]) -> None:
    store.append_jsonl(C.LIVE_ORDERS_PATH, row)


def kill_switch_active() -> bool:
    return C.KILL_SWITCH_PATH.exists()
