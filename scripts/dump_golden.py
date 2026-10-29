"""
python -m scripts.dump_golden [--end YYYY-MM-DD] [--years 4] [--out engine/tests/golden]

Runs the existing Python indicator code and layer1/layer2 score() over a fixed
set of ~70 cases and writes golden CSVs for the C++ engine tests.

Each case's input OHLCV is written next to its indicator series, so the
engine tests can consume the goldens without fetching anything.

Output layout (under --out):
    config.csv           key,value  -- every config threshold layers 1/2 read
    cases.csv            one row per case: category, source, rows, first/last date
    results.csv          one row per case: flattened layer1 + layer2 score() output
    series/<case>.csv    date, OHLCV, OBV, ma20, ma50, ma200, roc10, adx, di_p, di_n
    etf/<ETF>.csv        date, OHLCV, OBV for each sector ETF passed to layer2

The 2024-25 IPOs all have >= 200 rows by now, so the "ipo_new" cases are the
newest listings in the universe: up to DISCOVER_UNDER_L1 with fewer rows than
Layer 1 needs and DISCOVER_UNDER_L2 with fewer than Layer 2 needs.  They are
pinned in CASES so reruns are reproducible; --discover scans the cached
universe again and adds whatever it picks.

Case names are the ticker, or TICKER__nN for a copy truncated to its last N
rows (exercises the L1 22-row and L2 200-row minimums on a known-good series).

Archetypes 2 and 3 rarely fire on the latest bar, so ARCH_CASES cuts real
series at dates where they do (TICKER__toYYYY-MM-DD; found by scanning
engine/data/history_panel.npz with layer2.score()).  The sector ETF passed in
is cut at the same date, so etf/<ETF>.csv must be truncated to the case's
as_of before scoring.

SYN_* cases are synthetic series built to land exactly on a threshold:
breakout pct == BREAKOUT_PCT_MIN and volume == BREAKOUT_VOLUME_RATIO x average
(fires), the close one ulp below or volume 1 share below (does not fire),
and ADX == ADX_MIN exactly
(sig A off, since the check is strict) next to ADX above it.  Their expected
results are asserted when the goldens are written, and listed in cases.csv.

Floats are written as Python repr (shortest string that round-trips to the
same double, so strtod recovers the exact value); NaN is "nan";
None is an empty field; bools are 0/1.  Layer score() outputs are rounded
inside the backend (3 or 2 dp) and are written exactly as returned.
"""

import argparse
import csv
import logging
import math
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from backend import config
from backend.data.fetcher_price import _compute_obv
from backend.scoring import layer1_volume as layer1
from backend.scoring import layer2_structure as layer2
from scripts._history import fetch_history, last_completed_session, load_universe, trading_calendar

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
logger = logging.getLogger("dump_golden")

# ---------------------------------------------------------------------------
# Case list: (ticker, sector ETF, category)
# ---------------------------------------------------------------------------

CASES: list[tuple[str, str, str]] = [
    # Recent IPOs / listings (2024-2025) -- many have < 200 rows, some < 22
    ("CRWV", "XLK", "ipo"), ("CRCL", "XLF", "ipo"), ("FIG",  "XLK", "ipo"),
    ("KLAR", "XLF", "ipo"), ("CHYM", "XLF", "ipo"), ("ETOR", "XLF", "ipo"),
    ("BLSH", "XLF", "ipo"), ("SAIL", "XLK", "ipo"), ("GLXY", "XLF", "ipo"),
    ("VG",   "XLE", "ipo"), ("SFD",  "XLP", "ipo"), ("KLC",  "XLY", "ipo"),
    ("LINE", "XLRE", "ipo"), ("WAY", "XLV", "ipo"),
    # Short histories (2023-2024 IPOs and spinoffs) -- start inside the 4y window
    ("RDDT", "XLC", "short"), ("ALAB", "XLK", "short"), ("TEM",  "XLV", "short"),
    ("RBRK", "XLK", "short"), ("LOAR", "XLI", "short"), ("VIK",  "XLY", "short"),
    ("CAVA", "XLY", "short"), ("ARM",  "XLK", "short"), ("CART", "XLY", "short"),
    ("BIRK", "XLY", "short"), ("KVYO", "XLK", "short"), ("KVUE", "XLP", "short"),
    ("GEV",  "XLI", "short"), ("SOLV", "XLV", "short"), ("VLTO", "XLI", "short"),
    # Normal, full-history names across all 11 sectors
    ("AAPL", "XLK", "normal"), ("MSFT", "XLK", "normal"), ("NVDA", "XLK", "normal"),
    ("SMCI", "XLK", "normal"), ("AMZN", "XLY", "normal"), ("TSLA", "XLY", "normal"),
    ("META", "XLC", "normal"), ("JPM",  "XLF", "normal"), ("BAC",  "XLF", "normal"),
    ("XOM",  "XLE", "normal"), ("JNJ",  "XLV", "normal"), ("PG",   "XLP", "normal"),
    ("COST", "XLP", "normal"), ("CAT",  "XLI", "normal"), ("NEE",  "XLU", "normal"),
    ("PLD",  "XLRE", "normal"), ("LIN", "XLB", "normal"),
    # Newest listings, picked by --discover on 2026-10-28 and pinned so reruns are stable:
    # SATS has fewer rows than L1 needs, the rest fall between the L1 and L2 minimums
    ("SATS", "", "ipo_new"), ("AIBZ", "", "ipo_new"), ("CBRS", "", "ipo_new"),
    ("ALMR", "", "ipo_new"), ("SUNB", "", "ipo_new"), ("UUUG", "", "ipo_new"),
]

# Truncated copies: (ticker, keep last N rows).  Boundaries come from the layers:
# L1 needs OBV_LOOKBACK_DAYS + 2 rows, L2 needs layer2._MIN_ROWS rows.
_L1_MIN = config.OBV_LOOKBACK_DAYS + 2
_L2_MIN = layer2._MIN_ROWS
TRUNCATIONS: list[tuple[str, int]] = [
    ("AAPL", _L1_MIN - 1), ("AAPL", _L1_MIN),
    ("AAPL", _L2_MIN - 1), ("AAPL", _L2_MIN), ("AAPL", _L2_MIN + 1),
]

# Real (ticker, sector ETF, as_of, expected archetype) where archetype 1 does not
# fire first.  Found by a layer2.score() scan of the 2026-10-28 panel.
ARCH_CASES: list[tuple[str, str, str, int]] = [
    ("ABBV", "XLV", "2023-09-22", 2),
    ("AA",   "XLB", "2024-04-04", 2),
    ("AAL",  "XLI", "2024-03-12", 2),   # golden cross with zero momentum points
    ("ADBE", "XLK", "2024-06-18", 3),
    ("AAP",  "XLY", "2025-05-27", 3),   # +57% gap
    ("A",    "XLV", "2023-11-24", 3),   # +8.7%, just over BREAKOUT_PCT_MIN
]

DISCOVER_UNDER_L1 = 3
DISCOVER_UNDER_L2 = 5

CONFIG_KEYS = [
    "ALPACA_IEX_VOLUME_MULTIPLIER",
    "OBV_LOOKBACK_DAYS", "VOLUME_SURGE_RATIO", "RVOL_THRESHOLD",
    "UP_DOWN_VOL_RATIO_MIN", "LAYER1_MIN_SIGNALS",
    "ADX_MIN", "ROC_PERIOD", "MA_SLOPE_LOOKBACK", "GOLDEN_CROSS_LOOKBACK",
    "BREAKOUT_PCT_MIN", "BREAKOUT_VOLUME_RATIO", "BREAKOUT_HOLD_SESSIONS",
    "DISTRIBUTION_DAY_MAX", "UPPER_RANGE_CLOSE_MIN", "MA_BONUS_PER_LEVEL",
    "L2_MOMENTUM_A_SCORE", "L2_MOMENTUM_B_SCORE",
]

OHLCV_COLS = ["Open", "High", "Low", "Close", "Volume", "OBV"]
INDICATOR_COLS = ["ma20", "ma50", "ma200", "roc10", "adx", "di_p", "di_n"]

RESULT_COLS = [
    "case", "ticker", "category", "sector_etf", "rows", "as_of",
    # layer 1
    "l1_score", "l1_A", "l1_B", "l1_C", "l1_D", "l1_E",
    "l1_vol_ratio", "l1_rvol", "l1_up_down_ratio", "l1_block_trades", "l1_signals_fired",
    # layer 2
    "l2_archetype", "l2_archetype_score", "l2_momentum_score", "l2_total",
    "l2_A", "l2_B", "l2_C_pts", "l2_roc10", "l2_adx", "l2_di_p", "l2_di_n",
    "l2_dist_days", "l2_close_pct",
    "l2_breakout_date", "l2_breakout_pct_chg", "l2_breakout_vol_ratio",
]


def _fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, float):
        return "nan" if math.isnan(v) else repr(float(v))   # float(): np.float64 reprs as "np.float64(x)"
    if hasattr(v, "item"):          # numpy scalar
        return _fmt(v.item())
    return str(v)


def _write_frame(path: Path, df: pd.DataFrame, cols: list[str]) -> None:
    with path.open("w", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["date", *cols])
        for idx, row in zip(df.index, df[cols].itertuples(index=False)):
            w.writerow([idx.strftime("%Y-%m-%d"), *(_fmt(float(v)) for v in row)])


def _score_case(case: str, ticker: str, category: str, etf: str,
                df: pd.DataFrame, etf_df: pd.DataFrame) -> dict:
    l1 = layer1.score({"ticker": case, "ohlcv": df, "block_trades": 0})
    l2 = layer2.score({
        "ticker":           case,
        "ohlcv":            df,
        "sector_etf_ohlcv": etf_df,
        "sector_rs_rank":   None,
    })
    s1, s2 = l1["signals"], l2["signals"]
    arch_info = s2.get("arch_info", {})

    row = {
        "case": case, "ticker": ticker, "category": category, "sector_etf": etf,
        "rows": len(df), "as_of": df.index[-1].strftime("%Y-%m-%d"),
        "l1_score": l1["score"],
        "l2_archetype": l2["archetype"],
        "l2_archetype_score": l2["archetype_score"],
        "l2_momentum_score": l2["momentum_score"],
        "l2_total": l2["total"],
        "l2_breakout_date": arch_info.get("breakout_date"),
        "l2_breakout_pct_chg": arch_info.get("pct_chg"),
        "l2_breakout_vol_ratio": arch_info.get("vol_ratio"),
    }
    for k in ("A", "B", "C", "D", "E", "vol_ratio", "rvol", "up_down_ratio",
              "block_trades", "signals_fired"):
        row[f"l1_{k}"] = s1.get(k)
    for k in ("A", "B", "C_pts", "roc10", "adx", "di_p", "di_n", "dist_days", "close_pct"):
        row[f"l2_{k}"] = s2.get(k)
    return row


def _spread(items: list, n: int) -> list:
    """n items evenly spaced through a sorted list (all of them if len <= n)."""
    if len(items) <= n:
        return items
    step = (len(items) - 1) / (n - 1) if n > 1 else 0
    return [items[round(i * step)] for i in range(n)]


def _frame(index: pd.DatetimeIndex, close: list[float], high: list[float],
           low: list[float], volume: list[float]) -> pd.DataFrame:
    df = pd.DataFrame({"Open": close, "High": high, "Low": low, "Close": close,
                       "Volume": volume}, index=index, dtype=float)
    df["OBV"] = _compute_obv(df)
    return df


def _check_synthetic_assumptions() -> None:
    """The SYN_* series hard-code the values that sit exactly on the default thresholds."""
    need = {"BREAKOUT_PCT_MIN": 0.08, "BREAKOUT_VOLUME_RATIO": 2.0, "BREAKOUT_HOLD_SESSIONS": 2,
            "OBV_LOOKBACK_DAYS": 20, "ADX_MIN": 20}
    off = {k: getattr(config, k) for k, v in need.items() if getattr(config, k) != v}
    if off:
        raise RuntimeError(f"SYN_* cases assume default thresholds; overridden: {off}")
    # 8.0 / 100.0 rounds to the same double as the literal 0.08
    assert (108.0 - 100.0) / 100.0 == config.BREAKOUT_PCT_MIN


def _breakout_series(index: pd.DatetimeIndex, bo_close: float, bo_volume: float) -> pd.DataFrame:
    """
    Declining series (so archetypes 1 and 2 cannot fire) ending in a breakout
    from 100 to bo_close, held for 2 sessions at 108.  Base volume 900K: the
    20-day average before today then includes the breakout bar, so
    (19 * 900K + 1.9M) / 20 = 950K and 1.9M is exactly 2.0x.
    """
    n = len(index)
    k = n - 3                                   # breakout bar; 2 hold bars follow
    close = [100.0 + 0.25 * (k - 1 - j) for j in range(k)] + [bo_close, 108.0, 108.0]
    volume = [900_000.0] * n
    volume[k] = bo_volume
    return _frame(index, close, [c + 0.5 for c in close], [c - 0.5 for c in close], volume)


def _adx_series(index: pd.DatetimeIndex, down_step: float) -> pd.DataFrame:
    """
    Fixed 8-wide bars alternating +3 / -down_step.  Every TR is 8, +DM averages
    1.5 and -DM averages down_step / 2 over any 14 bars, all exact in binary.
    down_step=2: DI = 18.75 / 12.5, DX = 100 * 6.25 / 31.25 = 20.0 exactly.
    """
    low, lo = [], 100.0
    for j in range(len(index)):
        if j:
            lo += 3.0 if j % 2 else -down_step
        low.append(lo)
    high = [x + 8.0 for x in low]
    close = [x + 4.0 for x in low]
    return _frame(index, close, high, low, [1_000_000.0] * len(index))


def synthetic_cases(end: date) -> list[tuple[str, pd.DataFrame, dict]]:
    """(case, frame, expected result fields).  "series_adx" checks the unrounded indicator."""
    _check_synthetic_assumptions()
    index = trading_calendar(end - timedelta(days=400), end)[-260:]
    below_108 = math.nextafter(108.0, 0.0)
    return [
        ("SYN_breakout_at_threshold", _breakout_series(index, 108.0, 1_900_000.0),
         {"l2_archetype": 3, "l2_breakout_pct_chg": 0.08, "l2_breakout_vol_ratio": 2.0}),
        ("SYN_breakout_pct_below", _breakout_series(index, below_108, 1_900_000.0),
         {"l2_archetype": None}),
        ("SYN_breakout_vol_below", _breakout_series(index, 108.0, 1_899_999.0),
         {"l2_archetype": None}),
        ("SYN_adx_at_threshold", _adx_series(index, 2.0),
         {"series_adx": 20.0, "l2_adx": 20.0, "l2_A": False}),
        ("SYN_adx_above_threshold", _adx_series(index, 1.75),
         {"l2_A": True}),
    ]


def discover_recent_listings(end: date, exclude: set[str]) -> list[str]:
    """Newest listings in the cached universe, bucketed by the L1/L2 row minimums."""
    universe = [t for t in load_universe() if t not in exclude]
    lookback = end - timedelta(days=400)   # comfortably more than _L2_MIN sessions
    hist, _ = fetch_history(universe, lookback, end)
    rows = sorted((len(df), t) for t, df in hist.items() if df.index[-1] == pd.Timestamp(end))

    under_l1 = [t for n, t in rows if n < _L1_MIN]
    under_l2 = [t for n, t in rows if _L1_MIN <= n < _L2_MIN]
    picked = _spread(under_l1, DISCOVER_UNDER_L1) + _spread(under_l2, DISCOVER_UNDER_L2)
    logger.info(
        "Discovery: %d universe tickers < %d rows, %d in [%d, %d); picked %s",
        len(under_l1), _L1_MIN, len(under_l2), _L1_MIN, _L2_MIN, picked,
    )
    return picked


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    ap.add_argument("--end", type=date.fromisoformat, default=None,
                    help="last session to include (default: last completed NYSE session)")
    ap.add_argument("--years", type=float, default=4.0)
    ap.add_argument("--out", type=Path, default=Path("engine/tests/golden"))
    ap.add_argument("--discover", action="store_true",
                    help="also scan the universe for listings shorter than the L1/L2 minimums")
    args = ap.parse_args()

    end = args.end or last_completed_session()
    start = end - timedelta(days=round(args.years * 365.25))
    etfs = sorted(({etf for _, etf, _ in CASES} | {etf for _, etf, _, _ in ARCH_CASES}) - {""})
    cases = list(CASES)
    if args.discover:
        known = {t for t, _, _ in CASES} | set(etfs)
        cases += [(t, "", "ipo_new") for t in discover_recent_listings(end, known)]
    tickers = sorted({t for t, _, _ in cases} | {t for t, _, _, _ in ARCH_CASES})

    logger.info("Fetching %d tickers + %d ETFs, %s .. %s", len(tickers), len(etfs), start, end)
    data, source = fetch_history(tickers + etfs, start, end)

    out: Path = args.out
    (out / "series").mkdir(parents=True, exist_ok=True)
    (out / "etf").mkdir(parents=True, exist_ok=True)

    with (out / "config.csv").open("w", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["key", "value"])
        for k in CONFIG_KEYS:
            w.writerow([k, _fmt(getattr(config, k))])
        w.writerow(["L2_MIN_ROWS", layer2._MIN_ROWS])

    for etf in etfs:
        if etf in data:
            _write_frame(out / "etf" / f"{etf}.csv", data[etf], OHLCV_COLS)
        else:
            logger.warning("No data for sector ETF %s -- arch 4 will see an empty frame", etf)

    def _recompute_obv(df: pd.DataFrame) -> pd.DataFrame:
        # Re-run the backend OBV on a cut frame, as a fetch ending there would
        df = df[OHLCV_COLS[:-1]].copy()
        df["OBV"] = _compute_obv(df)
        return df

    # Jobs: (case, ticker, category, etf, frame, source, expected fields, strict)
    # strict: a mismatch with the expected fields is an error (synthetic) vs a warning (real)
    jobs: list[tuple[str, str, str, str, pd.DataFrame, str, dict, bool]] = []
    skipped: list[str] = []
    for ticker, etf, category in cases:
        if ticker not in data:
            skipped.append(ticker)
            continue
        jobs.append((ticker, ticker, category, etf, data[ticker], source[ticker], {}, False))
    etf_of = {t: e for t, e, _ in CASES}
    for ticker, n in TRUNCATIONS:
        if ticker in data:
            jobs.append((f"{ticker}__n{n}", ticker, "truncated", etf_of[ticker],
                         _recompute_obv(data[ticker].iloc[-n:]), source[ticker], {}, False))
    for ticker, etf, as_of, arch in ARCH_CASES:
        if ticker not in data:
            skipped.append(ticker)
            continue
        df = _recompute_obv(data[ticker].loc[:as_of])
        jobs.append((f"{ticker}__to{as_of}", ticker, f"arch{arch}", etf, df, source[ticker],
                     {"l2_archetype": arch}, False))
    for case, df, expect in synthetic_cases(end):
        jobs.append((case, case, "synthetic", "", df, "synthetic", expect, True))

    results: list[dict] = []
    case_rows: list[list] = []
    for case, ticker, category, etf, df, src, expect, strict in jobs:
        ind = layer2._add_indicators(df)
        _write_frame(out / "series" / f"{case}.csv", ind, OHLCV_COLS + INDICATOR_COLS)
        as_of = df.index[-1]
        etf_df = data[etf].loc[:as_of] if etf in data else pd.DataFrame()   # no look-ahead
        row = _score_case(case, ticker, category, etf, df, etf_df)
        results.append(row)

        got = {k: (float(ind["adx"].iloc[-1]) if k == "series_adx" else row.get(k)) for k in expect}
        bad = {k: (expect[k], got[k]) for k in expect if got[k] != expect[k]}
        if bad:
            msg = f"{case}: expected != got {bad}"
            if strict:
                raise AssertionError(msg)
            logger.warning(msg)
        case_rows.append([case, ticker, category, etf, src, len(df),
                          df.index[0].strftime("%Y-%m-%d"), as_of.strftime("%Y-%m-%d"),
                          " ".join(f"{k}={_fmt(v)}" for k, v in expect.items())])

    with (out / "results.csv").open("w", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(RESULT_COLS)
        for r in results:
            w.writerow([_fmt(r.get(c)) for c in RESULT_COLS])

    with (out / "cases.csv").open("w", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["case", "ticker", "category", "sector_etf", "source", "rows",
                    "first_date", "last_date", "expect"])
        w.writerows(case_rows)

    by_cat: dict[str, int] = {}
    for r in results:
        by_cat[r["category"]] = by_cat.get(r["category"], 0) + 1
    logger.info("Wrote %d cases to %s  %s", len(results), out, by_cat)
    if skipped:
        logger.warning("Skipped (no data from Alpaca or yfinance): %s", ", ".join(skipped))


if __name__ == "__main__":
    main()
