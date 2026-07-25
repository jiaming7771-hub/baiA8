"""OKX 永续行情适配。"""

from __future__ import annotations

from typing import Any

from simlab import config
from simlab.market.http import fetch_json


def fetch_swap_bases() -> set[str]:
    data = fetch_json("/api/v5/public/instruments?instType=SWAP", config.OKX_HOSTS)
    bases: set[str] = set()
    for row in (data or {}).get("data") or []:
        if row.get("state") != "live":
            continue
        if (row.get("settleCcy") or "").upper() != "USDT":
            continue
        inst = row.get("instId") or ""
        parts = inst.split("-")
        if len(parts) >= 2 and parts[1] == "USDT":
            base = parts[0].upper()
            if base and base not in config.EXCLUDE_BASES:
                bases.add(base)
    return bases


def fetch_swap_tickers() -> list[dict[str, Any]]:
    data = fetch_json("/api/v5/market/tickers?instType=SWAP", config.OKX_HOSTS)
    return (data or {}).get("data") or []


def fetch_funding(inst_id: str) -> float | None:
    data = fetch_json(
        f"/api/v5/public/funding-rate?instId={inst_id}", config.OKX_HOSTS
    )
    rows = (data or {}).get("data") or []
    if not rows:
        return None
    try:
        return float(rows[0].get("fundingRate"))
    except (TypeError, ValueError):
        return None


def parse_usdt_swap_base(inst_id: str) -> str | None:
    parts = (inst_id or "").split("-")
    if len(parts) >= 3 and parts[1] == "USDT" and parts[2] == "SWAP":
        base = parts[0].upper()
        if base and base not in config.EXCLUDE_BASES:
            return base
    return None
