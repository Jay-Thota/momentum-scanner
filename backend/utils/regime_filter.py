"""
backend/utils/regime_filter.py

Session-cached market regime based on SPX vs its 200-day moving average.
Import get_regime() directly from this module; result is cached for the
lifetime of the process to avoid redundant yfinance calls during a scan run.
"""

import logging
import time as _time

import yfinance as yf

from backend import config

logger = logging.getLogger(__name__)

_regime_cache: dict | None = None


def get_regime(force_refresh: bool = False) -> dict:
    """
    Classify the current market regime from SPX daily data.

    Bull when SPX is above its 200-day MA AND the 200-day MA slope is >= 0.
    Bear otherwise.

    Returns:
        {
            "regime":      "bull" | "bear",
            "spx_price":   float | None,
            "spx_200ma":   float | None,
            "above_200ma": bool | None,
            "ma_slope":    float | None,   -- fractional slope over MA_SLOPE_LOOKBACK sessions
        }

    Cached for the session; pass force_refresh=True to recompute.
    """
    global _regime_cache
    if _regime_cache is not None and not force_refresh:
        return _regime_cache

    try:
        for _attempt in range(3):
            try:
                df = yf.Ticker("^GSPC").history(period="1y", auto_adjust=True)
                break
            except Exception as _exc:
                if _attempt < 2 and "Too Many Requests" in str(_exc):
                    _time.sleep(2.0 ** _attempt)
                    continue
                raise
        if df.empty or len(df) < 201 + config.MA_SLOPE_LOOKBACK:
            logger.warning("SPX history too short (%d rows) -- defaulting to bull", len(df))
            _regime_cache = _default_regime()
            return _regime_cache

        close = df["Close"]
        ma200 = close.rolling(200).mean()

        spx_price   = float(close.iloc[-1])
        spx_200ma   = float(ma200.iloc[-1])
        above_200ma = spx_price > spx_200ma

        lookback  = config.MA_SLOPE_LOOKBACK
        ma_old    = float(ma200.iloc[-(lookback + 1)])
        ma_slope  = (spx_200ma - ma_old) / ma_old if ma_old != 0 else 0.0

        regime = "bull" if (above_200ma and ma_slope >= config.SECTOR_SLOPE_MIN) else "bear"

        _regime_cache = {
            "regime":      regime,
            "spx_price":   round(spx_price, 2),
            "spx_200ma":   round(spx_200ma, 2),
            "above_200ma": above_200ma,
            "ma_slope":    round(ma_slope, 6),
        }
        logger.info(
            "Regime: %s  SPX=%.2f  200MA=%.2f  slope=%+.4f%%",
            regime, spx_price, spx_200ma, ma_slope * 100,
        )

    except Exception as exc:
        logger.warning("get_regime failed: %s -- defaulting to bull", exc)
        _regime_cache = _default_regime()

    return _regime_cache


def _default_regime() -> dict:
    return {
        "regime":      "bull",
        "spx_price":   None,
        "spx_200ma":   None,
        "above_200ma": None,
        "ma_slope":    None,
    }
