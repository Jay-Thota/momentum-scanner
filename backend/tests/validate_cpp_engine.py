"""
backend/tests/validate_cpp_engine.py

Runs Step 4 (layer 1 + layer 2) through scorer.score_l1l2 on both paths, the
Python loop and the C++ engine, and asserts identical results:
  - the 68 golden series (engine/tests/golden/series)
  - edge cases built from them: NaN volume on a breakout day, NaN closes,
    NaN high/low, zero volume, dropped days, tz-aware UTC timestamps
  - the real universe from engine/data/history_panel.npz when present, at the
    production lookback (last ~300 calendar days) and at full 4-year length

"Identical" means the same keys in the same order, equal ints/bools/strings,
bit-equal floats (NaN matching NaN, -0.0 distinct from 0.0), and the same
signals JSON as save_results_to_db would store.

Requires the engine: pip install -e engine
Run with:
    python -m backend.tests.validate_cpp_engine
"""

import json
import logging
import math
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.ERROR, format="%(levelname)-8s %(name)s  %(message)s")

from backend.data.fetcher_price import _compute_obv
from backend.scoring.scorer import score_l1l2

SEP = "=" * 68
GOLDEN = Path("engine/tests/golden/series")
PANEL = Path("engine/data/history_panel.npz")
PRODUCTION_SESSIONS = 206   # what Alpaca's 300-calendar-day lookback returns


def _frame(df: pd.DataFrame) -> pd.DataFrame:
    df = df[["Open", "High", "Low", "Close", "Volume"]].astype(float).copy()
    df["OBV"] = _compute_obv(df)
    return df


def golden_frames() -> dict[str, pd.DataFrame]:
    out = {}
    for f in sorted(GOLDEN.glob("*.csv")):
        df = pd.read_csv(f, index_col="date", parse_dates=True, float_precision="round_trip")
        out[f.stem] = _frame(df)
    return out


def edge_frames(g: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    out = {}

    adbe = g["ADBE__to2024-06-18"].copy()
    adbe.loc["2024-06-14", "Volume"] = np.nan             # NaN volume on the breakout day
    out["EDGE_breakout_nan_volume"] = _frame(adbe)

    a = g["AAPL"].copy()
    a.iloc[-5, a.columns.get_loc("Close")] = np.nan       # NaN close inside every window
    out["EDGE_nan_close"] = _frame(a)

    a = g["AAPL"].copy()
    a.iloc[-1, [a.columns.get_loc("High"), a.columns.get_loc("Low")]] = np.nan
    out["EDGE_nan_high_low_today"] = _frame(a)

    a = g["AAPL"].copy()
    a.iloc[-21:, a.columns.get_loc("Volume")] = 0.0      # avg volume 0 -> vol_ratio 0.0
    out["EDGE_zero_volume"] = _frame(a)

    a = g["AAPL"].copy()
    a.iloc[-30:-20, a.columns.get_loc("Volume")] = np.nan   # NaNs in the volume windows
    a.iloc[-15, a.columns.get_loc("Volume")] = np.nan
    out["EDGE_nan_volume_windows"] = _frame(a)

    a = g["AAPL"].drop(g["AAPL"].index[[-40, -41, -300]])  # missing days, as Alpaca returns them
    out["EDGE_dropped_days"] = _frame(a)

    adbe = g["ADBE__to2024-06-18"].copy()                 # Alpaca-style UTC timestamps
    adbe.index = adbe.index.tz_localize("UTC") + pd.Timedelta(hours=4)
    out["EDGE_tz_aware_breakout"] = _frame(adbe)
    return out


def panel_frames(sessions: int | None) -> dict[str, pd.DataFrame]:
    """Each symbol as Alpaca would return it: real bars only, no NaN rows."""
    p = np.load(PANEL)
    # Each p[key] read decompresses the whole array, so read each one once
    fields = {f: p[f.lower()] for f in ("Open", "High", "Low", "Close", "Volume")}
    dates = pd.to_datetime(p["dates"].astype(str))
    sl = slice(-sessions, None) if sessions else slice(None)
    out = {}
    for i, sym in enumerate(p["symbols"]):
        df = pd.DataFrame({f: a[i, sl] for f, a in fields.items()}, index=dates[sl])
        df = df[df["Close"].notna()]
        if len(df):
            out[str(sym)] = _frame(df)
    return out


def alpaca_raw_bars(frames: dict[str, pd.DataFrame]) -> dict[str, list[dict]]:
    """
    Each frame as the Alpaca bars API returns it with raw_data=True: dicts with
    an ISO "t" at midnight ET in UTC and an integer "v" (the frame's volume as
    the raw IEX count; both paths then apply ALPACA_IEX_VOLUME_MULTIPLIER).
    """
    out = {}
    for t, df in frames.items():
        utc = pd.DatetimeIndex(df.index).tz_localize("America/New_York").tz_convert("UTC")
        stamps = utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        out[t] = [{"c": c, "h": h, "l": lo, "n": 0, "o": o, "t": s, "v": int(v), "vw": c}
                  for s, o, h, lo, c, v in zip(stamps, df["Open"], df["High"], df["Low"],
                                               df["Close"], df["Volume"])]
    return out


def run_alpaca_arrays(name: str, frames: dict[str, pd.DataFrame], fallback: dict[str, pd.DataFrame]) -> bool:
    """
    USE_CPP_ENGINE's Step 1-4 data path vs the original, from the same API
    response: raw JSON bars -> OhlcvBatch (+ Yahoo fallback merged in) vs
    alpaca-py Bar models built from that JSON -> DataFrames (+ the same
    fallback). Checks the rebuilt DataFrames, Step 2's tickers and Step 4.
    """
    from alpaca.data.models import Bar

    from backend.data.fetcher_price import bars_to_frame
    from backend.data.ohlcv_batch import LazyOhlcvMap, OhlcvBatch
    from backend.scoring.scorer import l0_price_volume_pass

    raw = alpaca_raw_bars(frames)
    tickers = sorted(set(raw) | set(fallback))
    old = {t: bars_to_frame([Bar(t, d) for d in rows]) for t, rows in raw.items()}
    old.update(fallback)
    t0 = time.perf_counter()
    lazy = LazyOhlcvMap(OhlcvBatch.from_raw_bars(raw, list(raw)))
    lazy.update(fallback)
    t_build = time.perf_counter() - t0

    diffs: list[str] = []
    pv_old, pv_new = l0_price_volume_pass(tickers, old), l0_price_volume_pass(tickers, lazy)
    if pv_old != pv_new:
        diffs.append(f"Step 2 differs: {len(pv_old)} vs {len(pv_new)} tickers, "
                     f"e.g. {sorted(set(pv_old) ^ set(pv_new))[:5]}")
    py = score_l1l2(tickers, old, use_cpp=False)
    cpp = score_l1l2(tickers, lazy, use_cpp=True)
    for t in tickers:
        compare(py[t], cpp[t], t, diffs)
    for t in tickers:                       # rebuilt frames, checked last (builds them all)
        try:
            pd.testing.assert_frame_equal(lazy[t], old[t], check_exact=True)
        except AssertionError as exc:
            diffs.append(f"{t}: rebuilt DataFrame differs: {str(exc).splitlines()[0]}")

    status = "[PASS]" if not diffs else "[FAIL]"
    print(f"  {status}  {name}: {len(tickers)} tickers ({len(fallback)} via fallback), "
          f"{len(diffs)} differences  (bars -> arrays {t_build:.2f}s, step 2 pass {len(pv_new)})")
    for d in diffs[:10]:
        print(f"           {d}")
    return not diffs


def _is_bool(x) -> bool:
    return isinstance(x, (bool, np.bool_))


def compare(a, b, path: str, diffs: list[str]) -> None:
    if isinstance(a, dict) and isinstance(b, dict):
        if list(a) != list(b):
            diffs.append(f"{path}: keys {list(a)} != {list(b)}")
            return
        for k in a:
            compare(a[k], b[k], f"{path}.{k}", diffs)
    elif _is_bool(a) or _is_bool(b):
        if not (_is_bool(a) and _is_bool(b) and bool(a) == bool(b)):
            diffs.append(f"{path}: {a!r} != {b!r}")
    elif isinstance(a, (float, np.floating)) and isinstance(b, (float, np.floating)):
        same = (math.isnan(a) and math.isnan(b)) or (a == b and math.copysign(1, a) == math.copysign(1, b))
        if not same:
            diffs.append(f"{path}: {a!r} != {b!r}")
    elif a != b or type(a) is not type(b):
        diffs.append(f"{path}: {a!r} ({type(a).__name__}) != {b!r} ({type(b).__name__})")


def run(name: str, frames: dict[str, pd.DataFrame]) -> bool:
    tickers = sorted(frames)
    t0 = time.perf_counter()
    py = score_l1l2(tickers, frames, use_cpp=False)
    t1 = time.perf_counter()
    cpp = score_l1l2(tickers, frames, use_cpp=True)
    t2 = time.perf_counter()

    diffs: list[str] = []
    for t in tickers:
        compare(py[t], cpp[t], t, diffs)
        # What save_results_to_db stores must match too (numpy bools become "True")
        for layer in ("l1", "l2"):
            a = json.dumps(py[t][layer]["signals"], default=str)
            b = json.dumps(cpp[t][layer]["signals"], default=str)
            if a != b:
                diffs.append(f"{t}.{layer}: stored JSON differs: {a} != {b}")
    arch = pd.Series([py[t]["l2"]["archetype"] for t in tickers]).value_counts(dropna=False).to_dict()
    status = "[PASS]" if not diffs else "[FAIL]"
    print(f"  {status}  {name}: {len(tickers)} tickers, {len(diffs)} differences"
          f"  (python {t1 - t0:.2f}s, c++ {t2 - t1:.3f}s)  archetypes {arch}")
    for d in diffs[:10]:
        print(f"           {d}")
    return not diffs


def main() -> None:
    import mscan_engine  # noqa: F401  -- fail fast with ImportError if not built

    print(f"\n{SEP}\n  Step 4 parity: Python layers vs C++ engine\n{SEP}")
    g = golden_frames()
    ok = run("golden series", g)
    ok &= run("edge cases", edge_frames(g))
    # Yahoo-style fallback rows: tz-naive daily index, longer than the Alpaca rows
    fallback = {f"YH_{k}": g[k] for k in ("AAPL", "ADBE__to2024-06-18", "SATS")}
    ok &= run_alpaca_arrays("alpaca bars -> arrays, golden series", g, fallback)
    if PANEL.exists():
        prod = panel_frames(PRODUCTION_SESSIONS)
        ok &= run(f"real universe, last {PRODUCTION_SESSIONS} sessions", prod)
        ok &= run("real universe, full history", panel_frames(None))
        ok &= run_alpaca_arrays(f"alpaca bars -> arrays, real universe ({PRODUCTION_SESSIONS} sessions)",
                                prod, fallback)
    else:
        print(f"  [SKIP]  real universe: {PANEL} not found (python -m scripts.export_history)")
    print(f"\n{SEP}\n  {'ALL PASSED' if ok else 'FAILURES ABOVE'}\n{SEP}\n")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
