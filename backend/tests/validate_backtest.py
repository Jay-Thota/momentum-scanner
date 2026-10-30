"""
backend/tests/validate_backtest.py

Re-derives the C++ backtest's picks for sampled days with the Python scan
code and asserts they are identical (same tickers, same order, bit-equal
scores). For each day d:
  - every eligible ticker's bars up to d, exactly what the engine's
    series(i, d) holds (interior NaN days kept)
  - Step 2's gate (scorer.l0_price_volume_pass) and Step 4's layers
    (scorer._score_l1l2_python)
  - the regime from SPY with regime_filter.get_regime's formula
  - PV score = L1 score + L2 total, threshold by regime, top N by score
    with ties in file order

This checks the engine's streaming state, gate, regime and ranking against
the reference implementation end to end. Sampled days: the first and last
of the default window, one in the middle, and the first bear day.

Requires the engine (pip install -e engine) and engine/data/history.bin.
Run with:
    python -m backend.tests.validate_backtest
"""

import logging
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.ERROR, format="%(levelname)-8s %(name)s  %(message)s")

import mscan_engine

from backend import config
from backend.data.fetcher_price import _compute_obv
from backend.data.fetcher_sector import SECTOR_PEERS
from backend.scoring.cpp_engine import engine_config
from backend.scoring.scorer import _score_l1l2_python, l0_price_volume_pass
from scripts.read_history_bin import read_history_bin

SEP = "=" * 68
HISTORY = Path("engine/data/history.bin")
WARMUP = 252
TOP_N, MIN_SCORE, BEAR_MIN_SCORE = 10, 2.0, 2.5


def frame(h: dict, i: int, first: int, d: int) -> pd.DataFrame:
    sl = slice(first, d + 1)
    df = pd.DataFrame({f: np.asarray(h[f.lower()][i, sl], dtype=float)
                       for f in ("Open", "High", "Low", "Close", "Volume")},
                      index=pd.to_datetime(h["dates"][sl].astype(str)))
    df["OBV"] = _compute_obv(df)
    return df


def bear(spy_close: pd.Series) -> bool:
    """regime_filter.get_regime's rule on SPY; bull when history is too short."""
    if len(spy_close) < 201 + config.MA_SLOPE_LOOKBACK:
        return False
    ma200 = spy_close.rolling(200).mean()
    price, ma = float(spy_close.iloc[-1]), float(ma200.iloc[-1])
    ma_old = float(ma200.iloc[-(config.MA_SLOPE_LOOKBACK + 1)])
    slope = (ma - ma_old) / ma_old if ma_old != 0 else 0.0
    return not (price > ma and slope >= config.SECTOR_SLOPE_MIN)


def python_picks(h: dict, first: np.ndarray, eligible: list[int], d: int) -> tuple[list, bool]:
    symbols = h["symbols"]
    frames = {symbols[i]: frame(h, i, first[i], d) for i in eligible if first[i] <= d}
    passed = l0_price_volume_pass(list(frames), frames)
    scores = _score_l1l2_python(passed, frames)
    is_bear = bear(frame(h, 0, first[0], d)["Close"])
    threshold = BEAR_MIN_SCORE if is_bear else MIN_SCORE
    order = {s: k for k, s in enumerate(symbols)}
    pv = [(s["l1"]["score"] + s["l2"].get("total", 0.0), t) for t, s in scores.items()]
    ranked = sorted((x for x in pv if x[0] >= threshold), key=lambda x: (-x[0], order[x[1]]))
    return [(t, score) for score, t in ranked[:TOP_N]], is_bear


def main() -> None:
    if not HISTORY.exists():
        sys.exit(f"{HISTORY} not found: run python -m scripts.export_history")
    h = read_history_bin(HISTORY)
    dates, symbols = h["dates"], h["symbols"]
    close = h["close"]
    first = np.argmax(~np.isnan(close), axis=1)
    excluded = {"SPY", *SECTOR_PEERS}
    eligible = [i for i, s in enumerate(symbols) if s not in excluded]
    assert symbols[0] == "SPY"

    cfg = engine_config() | {"MIN_PRICE": config.MIN_PRICE, "MIN_AVG_VOLUME": config.MIN_AVG_VOLUME,
                             "SECTOR_SLOPE_MIN": config.SECTOR_SLOPE_MIN}
    start, last = WARMUP, len(dates) - 1
    spy = frame(h, 0, first[0], last)["Close"]
    first_bear = next((d for d in range(start, last + 1) if bear(spy.iloc[:d - first[0] + 1])), None)
    days = sorted({start, (start + last) // 2, last} | ({first_bear} if first_bear else set()))

    print(SEP)
    print("  C++ backtest picks vs the Python scan code")
    print(SEP)
    failures = 0
    for d in days:
        t0 = time.perf_counter()
        want, is_bear = python_picks(h, first, eligible, d)
        py_s = time.perf_counter() - t0
        r = mscan_engine.run_backtest(str(HISTORY), cfg, start=int(dates[d]), end=int(dates[d]),
                                      exclude=sorted(SECTOR_PEERS), top_n=TOP_N,
                                      min_pv_score=MIN_SCORE, bear_min_pv_score=BEAR_MIN_SCORE,
                                      horizons=[5], num_threads=0)
        got = [(p["ticker"], p["score"]) for p in r["picks"]]
        ok = got == want and r["bear_days"] == int(is_bear)
        failures += not ok
        label = f"{dates[d]} ({'bear' if is_bear else 'bull'})"
        print(f"  {'[PASS]' if ok else '[FAIL]'}  {label}: {len(got)} picks  "
              f"(python {py_s:.1f}s, c++ {r['elapsed_ms'] / 1000:.2f}s)  "
              f"{', '.join(f'{t} {s:g}' for t, s in got[:3])}{' ...' if len(got) > 3 else ''}")
        if not ok:
            print(f"          c++    {got}\n          python {want}")
    print(SEP)
    print("  ALL PASSED" if failures == 0 else f"  {failures} FAILED")
    print(SEP)
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
