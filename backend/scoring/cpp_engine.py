"""
backend/scoring/cpp_engine.py

Step 4 (layer 1 + layer 2 on OHLCV only) on the C++ engine in engine/.
Build it with `pip install -e engine`; scorer.py only imports this module
when config.USE_CPP_ENGINE is on.

The engine takes one (T, D) float64 array per field. Each row is a ticker's
bars in order, left-padded with NaN to the longest history. Rows are
right-aligned bar sequences, not one shared calendar, so a ticker with a
missing day is scored on exactly the rows Python would see. A parallel
int64 array of YYYYMMDD dates lets the engine report breakout dates the way
layer2 does (str(index)[:10]).

When run_daily_scan built a LazyOhlcvMap (USE_CPP_ENGINE on), the arrays
already exist and are passed as they are. A plain {ticker: DataFrame} dict is
converted first.

Public API:
    score_l1l2_cpp(tickers, ohlcv_map) -> {ticker: {"l1": dict, "l2": dict}}
"""

from collections.abc import Mapping

import numpy as np
import pandas as pd

import mscan_engine

from backend import config
from backend.data.ohlcv_batch import FIELDS, LazyOhlcvMap, OhlcvBatch

# Every config value layers 1 and 2 read, passed live so .env overrides apply
_CONFIG_KEYS = (
    "OBV_LOOKBACK_DAYS", "VOLUME_SURGE_RATIO", "RVOL_THRESHOLD",
    "UP_DOWN_VOL_RATIO_MIN", "LAYER1_MIN_SIGNALS",
    "ADX_MIN", "ROC_PERIOD", "MA_SLOPE_LOOKBACK", "GOLDEN_CROSS_LOOKBACK",
    "BREAKOUT_PCT_MIN", "BREAKOUT_VOLUME_RATIO", "BREAKOUT_HOLD_SESSIONS",
    "DISTRIBUTION_DAY_MAX", "UPPER_RANGE_CLOSE_MIN", "MA_BONUS_PER_LEVEL",
    "L2_MOMENTUM_A_SCORE", "L2_MOMENTUM_B_SCORE",
)


def engine_config() -> dict:
    return {k: getattr(config, k) for k in _CONFIG_KEYS}


def stack_ohlcv(
    tickers: list[str], ohlcv_map: Mapping[str, pd.DataFrame],
) -> tuple[dict[str, np.ndarray], np.ndarray]:
    """({field: (T, D) float64}, (T, D) int64 dates), right-aligned and NaN/0-padded."""
    if isinstance(ohlcv_map, LazyOhlcvMap):
        return ohlcv_map.batch.rows(tickers)
    return OhlcvBatch.from_frames(tickers, ohlcv_map).rows(tickers)


def score_l1l2_cpp(tickers: list[str], ohlcv_map: Mapping[str, pd.DataFrame]) -> dict[str, dict]:
    arrays, dates = stack_ohlcv(tickers, ohlcv_map)
    results = mscan_engine.score_l1l2_batch(
        list(tickers), *(arrays[f] for f in FIELDS), engine_config(), dates=dates,
    )
    return dict(zip(tickers, results))
