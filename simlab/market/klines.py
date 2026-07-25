"""K 线 → DataFrame。"""

from __future__ import annotations

import pandas as pd

from simlab.market import binance as bn


def klines_to_df(raw: list | None) -> pd.DataFrame:
    rows = []
    for k in raw or []:
        try:
            rows.append(
                {
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "volume": float(k[5]),
                }
            )
        except (TypeError, ValueError, IndexError):
            continue
    return pd.DataFrame(rows)


def load_mtf_frames(pair: str, limit: int = 120) -> dict[str, pd.DataFrame]:
    return {
        "df_15m": klines_to_df(bn.fetch_klines(pair, "15m", limit)),
        "df_1h": klines_to_df(bn.fetch_klines(pair, "1h", limit)),
        "df_4h": klines_to_df(bn.fetch_klines(pair, "4h", limit)),
    }


def latest_ohlc_15m(pair: str) -> dict[str, float] | None:
    raw = bn.fetch_klines(pair, "15m", 3)
    if not raw:
        return None
    k = raw[-1]
    try:
        return {
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
        }
    except (TypeError, ValueError, IndexError):
        return None
