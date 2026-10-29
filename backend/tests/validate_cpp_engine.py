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
and bit-equal floats (NaN matching NaN, -0.0 distinct from 0.0).

Requires the engine: pip install -e engine
Run with:
    python -m backend.tests.validate_cpp_engine
"""

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
    if PANEL.exists():
        ok &= run(f"real universe, last {PRODUCTION_SESSIONS} sessions", panel_frames(PRODUCTION_SESSIONS))
        ok &= run("real universe, full history", panel_frames(None))
    else:
        print(f"  [SKIP]  real universe: {PANEL} not found (python -m scripts.export_history)")
    print(f"\n{SEP}\n  {'ALL PASSED' if ok else 'FAILURES ABOVE'}\n{SEP}\n")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
