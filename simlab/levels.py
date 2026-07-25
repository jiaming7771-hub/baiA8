"""多周期量化挂单点位纯函数（生产契约：禁止改动算法体）。"""

from __future__ import annotations

from typing import Dict, Optional

import numpy as np
import pandas as pd


def calculate_advanced_trading_levels(
    df_4h: pd.DataFrame,
    df_1h: pd.DataFrame,
    df_15m: pd.DataFrame,
    *,
    atr_period: int = 14,
    bb_period: int = 20,
    defense_lookback: int = 20,
    recent_low_lookback: int = 5,
    volume_confirm_window: int = 5,
    volume_baseline_window: int = 20,
    panic_vol_multiplier: float = 1.5,
    atr_coeff_normal: float = 0.3,
    atr_coeff_panic: float = 0.5,
    stop_atr_mult: float = 1.5,
    take_profit_pct: float = 0.010,
    defense_buffer: float = 0.002,
    min_stop_buffer: float = 0.01,
    min_data_points: int = 30,
) -> Optional[Dict[str, float]]:
    try:
        required_cols = {'high', 'low', 'close'}
        for name, df in [('4h', df_4h), ('1h', df_1h), ('15m', df_15m)]:
            if df is None or len(df) < min_data_points or not required_cols.issubset(df.columns):
                return None

        df_4h = df_4h.copy()
        df_1h = df_1h.copy()
        df_15m = df_15m.copy()

        defense = float(df_4h['low'].iloc[-defense_lookback:].min())
        if np.isnan(defense) or defense <= 0:
            return None

        close_1h = df_1h['close']
        sma_1h = close_1h.rolling(bb_period, min_periods=bb_period).mean()
        std_1h = close_1h.rolling(bb_period, min_periods=bb_period).std(ddof=0)
        lower_band_1h = sma_1h - 2.0 * std_1h
        current_lower_band = float(lower_band_1h.iloc[-1])
        if np.isnan(current_lower_band):
            return None

        high = df_15m['high']
        low = df_15m['low']
        close = df_15m['close']
        volume = df_15m['volume'] if 'volume' in df_15m.columns else pd.Series(1.0, index=df_15m.index)

        prev_close = close.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs()
        ], axis=1).max(axis=1)

        atr = tr.rolling(window=atr_period, min_periods=atr_period).mean()
        current_atr = float(atr.iloc[-1])
        if np.isnan(current_atr) or current_atr <= 0:
            return None

        recent_low_15m = float(low.iloc[-recent_low_lookback:].min())

        vol_mean_recent = volume.iloc[-volume_confirm_window:].mean()
        vol_mean_base = volume.rolling(volume_baseline_window, min_periods=volume_baseline_window).mean().iloc[-1]
        is_panic_volume = (vol_mean_recent > panic_vol_multiplier * vol_mean_base) if not np.isnan(vol_mean_base) else False
        atr_coeff = atr_coeff_panic if is_panic_volume else atr_coeff_normal

        raw_entry = min(current_lower_band, recent_low_15m) - atr_coeff * current_atr
        entry = defense * (1.0 + defense_buffer) if raw_entry < defense else raw_entry

        raw_stop = entry - stop_atr_mult * current_atr
        min_allowed_stop = defense * (1.0 - min_stop_buffer)
        stop_loss = max(raw_stop, min_allowed_stop)

        take_profit = entry * (1.0 + take_profit_pct)

        if not (stop_loss < entry < take_profit) or entry <= 0:
            return None

        return {
            "defense": round(defense, 6),
            "lower_band": round(current_lower_band, 6),
            "entry": round(entry, 6),
            "stop_loss": round(stop_loss, 6),
            "take_profit": round(take_profit, 6),
            "atr": round(current_atr, 6),
            "is_panic_volume": bool(is_panic_volume),
        }
    except Exception:
        return None
