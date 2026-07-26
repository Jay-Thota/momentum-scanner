import logging

import pandas as pd

from backend import config

logger = logging.getLogger(__name__)


def score(data_bundle: dict) -> dict:
    """
    Layer 1: Volume surge.

    data_bundle keys:
        ticker       -- str, used only for logging
        ohlcv        -- pd.DataFrame from fetch_daily_ohlcv (includes OBV column)
        block_trades -- int, count of block trades today (0 when Alpaca unavailable)

    Returns:
        {
          "score":   float  (0.0 or 1.0),
          "signals": {
              "A": bool,           today_vol >= VOLUME_SURGE_RATIO * avg_vol_20d
              "B": bool,           vol_ratio > RVOL_THRESHOLD
              "C": bool,           up_vol / down_vol (20d) > UP_DOWN_VOL_RATIO_MIN
              "D": bool,           OBV at or above 20d rolling high
              "E": bool,           block_trade_count > 0
              "vol_ratio":    float,
              "rvol":         float,
              "up_down_ratio":float,
              "block_trades": int,
              "signals_fired":int,
          }
        }
    """
    ticker: str = data_bundle.get("ticker", "?")
    ohlcv: pd.DataFrame = data_bundle.get("ohlcv", pd.DataFrame())
    block_trades: int = int(data_bundle.get("block_trades", 0))

    empty = {"score": 0.0, "signals": {}}

    if ohlcv.empty or len(ohlcv) < config.OBV_LOOKBACK_DAYS + 2:
        logger.warning("Insufficient OHLCV data for Layer 1 scoring of %s (%d rows)", ticker, len(ohlcv))
        return empty

    lookback = config.OBV_LOOKBACK_DAYS  # 20

    today_row = ohlcv.iloc[-1]
    # 20 trading days immediately before today (not including today)
    prev_window = ohlcv.iloc[-(lookback + 1):-1]

    today_vol: float = float(today_row["Volume"])
    avg_vol_20d: float = float(prev_window["Volume"].mean())
    vol_ratio: float = today_vol / avg_vol_20d if avg_vol_20d > 0 else 0.0

    # -- Signal A: raw surge >= VOLUME_SURGE_RATIO (2.0x by default) ----------
    sig_A: bool = vol_ratio >= config.VOLUME_SURGE_RATIO

    # -- Signal B: RVOL > RVOL_THRESHOLD (1.5x by default) -------------------
    sig_B: bool = vol_ratio > config.RVOL_THRESHOLD

    # -- Signal C: up/down volume ratio over the last 20 trading days ---------
    # Use the full-dataframe diff so the first row of the window gets a valid value.
    close_diff = ohlcv["Close"].diff()
    recent_window = ohlcv.iloc[-lookback:].copy()
    recent_window["_diff"] = close_diff.iloc[-lookback:].values

    up_vol: float = recent_window.loc[recent_window["_diff"] > 0, "Volume"].sum()
    down_vol: float = recent_window.loc[recent_window["_diff"] < 0, "Volume"].sum()
    up_down_ratio: float = up_vol / max(down_vol, 1.0)
    sig_C: bool = up_down_ratio > config.UP_DOWN_VOL_RATIO_MIN

    # -- Signal D: today's OBV at or above its 20-day rolling high ------------
    recent_obv = ohlcv["OBV"].iloc[-lookback:]
    today_obv: float = float(ohlcv["OBV"].iloc[-1])
    obv_20d_max: float = float(recent_obv.max())
    sig_D: bool = today_obv >= obv_20d_max

    # -- Signal E: block trade count from Alpaca IEX --------------------------
    sig_E: bool = block_trades > 0

    signals_fired = int(sig_A) + int(sig_B) + int(sig_C) + int(sig_D) + int(sig_E)
    final_score = 1.0 if signals_fired >= config.LAYER1_MIN_SIGNALS else 0.0

    logger.debug(
        "%s vol_ratio=%.2f  A=%s B=%s C=%s D=%s E=%s  fired=%d  score=%.1f",
        ticker, vol_ratio, sig_A, sig_B, sig_C, sig_D, sig_E, signals_fired, final_score,
    )

    return {
        "score": final_score,
        "signals": {
            "A": sig_A,
            "B": sig_B,
            "C": sig_C,
            "D": sig_D,
            "E": sig_E,
            "vol_ratio":     round(vol_ratio, 3),
            "rvol":          round(vol_ratio, 3),
            "up_down_ratio": round(up_down_ratio, 3),
            "block_trades":  block_trades,
            "signals_fired": signals_fired,
        },
    }
