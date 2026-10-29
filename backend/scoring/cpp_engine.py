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

Public API:
    score_l1l2_cpp(tickers, ohlcv_map) -> {ticker: {"l1": dict, "l2": dict}}
"""

import numpy as np
import pandas as pd

import mscan_engine

from backend import config

_FIELDS = ("Open", "High", "Low", "Close", "Volume")

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
    tickers: list[str], ohlcv_map: dict[str, pd.DataFrame],
) -> tuple[dict[str, np.ndarray], np.ndarray]:
    """({field: (T, D) float64}, (T, D) int64 dates), right-aligned and NaN/0-padded."""
    frames = [ohlcv_map.get(t, pd.DataFrame()) for t in tickers]
    d = max((len(df) for df in frames), default=0)
    arrays = {f: np.full((len(tickers), d), np.nan) for f in _FIELDS}
    dates = np.zeros((len(tickers), d), dtype=np.int64)
    for i, df in enumerate(frames):
        k = len(df)
        if k == 0:
            continue
        for f in _FIELDS:
            arrays[f][i, d - k:] = df[f].to_numpy(dtype=np.float64)
        # Same calendar date str(ts)[:10] gives: the timestamp's own timezone
        idx = pd.DatetimeIndex(df.index)
        dates[i, d - k:] = idx.year * 10000 + idx.month * 100 + idx.day
    return arrays, dates


def score_l1l2_cpp(tickers: list[str], ohlcv_map: dict[str, pd.DataFrame]) -> dict[str, dict]:
    arrays, dates = stack_ohlcv(tickers, ohlcv_map)
    results = mscan_engine.score_l1l2_batch(
        list(tickers), *(arrays[f] for f in _FIELDS), engine_config(), dates=dates,
    )
    for r in results:
        # Python's layer1 signal C is a numpy bool (it compares a numpy.float64),
        # which save_results_to_db's json.dumps(default=str) stores as the string
        # "True"/"False". Match it so the stored JSON is the same on both paths.
        if "C" in r["l1"]["signals"]:
            r["l1"]["signals"]["C"] = np.bool_(r["l1"]["signals"]["C"])
    return dict(zip(tickers, results))
