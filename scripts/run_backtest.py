"""
python -m scripts.run_backtest [--start 2023-11-01] [--end 2026-10-28] [--fill open|close]
                               [--baseline random [--seeds 20]]

Backtests the PV-only part of the scanner (layers 1 + 2, the Step 2 price/
volume gate and the SPY regime filter) over engine/data/history.bin with the
C++ engine, and prints one row of stats per forward-return horizon.

The scan's other layers need data that is not available historically for
free (options flow, short interest, market cap, earnings dates), so this
validates part of the model, not the full 6.5-point score. The universe is
today's, so delisted names are missing and results lean optimistic. Bars
are split- and dividend-adjusted.

--baseline random also runs the backtest --seeds times with each day's
picks drawn at random from the same candidates (same L0 gate, regime
threshold and top_n), and prints the strategy next to the baseline's mean,
standard deviation and range across seeds.

Build the engine first (pip install -e engine) and export the history
(python -m scripts.export_history). Thresholds come from backend/config.py.
"""

import argparse
import csv
import statistics
import sys
from datetime import date
from pathlib import Path

import mscan_engine

from backend import config
from backend.data.fetcher_sector import SECTOR_PEERS
from backend.scoring.cpp_engine import engine_config
from scripts.read_history_bin import read_history_bin

HISTORY = Path(__file__).resolve().parents[1] / "engine" / "data" / "history.bin"
WARMUP_SESSIONS = 252   # layer 2 needs 200 bars; default start skips the first year
SPY = "SPY"             # benchmark and regime index, as in scripts/export_history.py


def yyyymmdd(s: str) -> int:
    return int(date.fromisoformat(s).strftime("%Y%m%d"))


def pct(x: float | None, digits: int = 2) -> str:
    return "n/a" if x is None else f"{x * 100:+.{digits}f}%"


def stats_table(stats: list[dict]) -> str:
    rows = [
        "| horizon | picks | hit rate vs SPY | mean return | mean excess | Sharpe | max drawdown |",
        "|---|---|---|---|---|---|---|",
    ]
    for s in stats:
        hit = "n/a" if s["hit_rate"] is None else f"{s['hit_rate'] * 100:.1f}%"
        sharpe = "n/a" if s["sharpe"] is None else f"{s['sharpe']:.2f}"
        dd = "n/a" if s["max_drawdown"] is None else f"{s['max_drawdown'] * 100:.1f}%"
        rows.append(f"| {s['horizon']}d | {s['n']:,} | {hit} | {pct(s['mean_return'])} | "
                    f"{pct(s['mean_excess'])} | {sharpe} | {dd} |")
    return "\n".join(rows)


# (key, label, scale, digits): hit rate, returns and drawdown in percent
BASELINE_FIELDS = (
    ("hit_rate", "hit rate vs SPY", 100, 2),
    ("mean_return", "mean return", 100, 3),
    ("mean_excess", "mean excess", 100, 3),
    ("sharpe", "Sharpe", 1, 3),
    ("max_drawdown", "max drawdown", 100, 2),
    ("n", "picks", 1, 0),
)


def baseline_table(strategy: list[dict], runs: list[list[dict]]) -> str:
    """Strategy vs random baseline: mean +/- sample stdev [min, max] across seeds."""
    rows = [
        f"| horizon | metric | strategy | random: mean +/- sd (n={len(runs)} seeds) "
        "| random: min .. max | strategy pctile |",
        "|---|---|---|---|---|---|",
    ]
    for hi, s in enumerate(strategy):
        for key, label, scale, digits in BASELINE_FIELDS:
            vals = [r[hi][key] for r in runs if r[hi][key] is not None]
            fmt = lambda x: "n/a" if x is None else f"{x * scale:.{digits}f}"   # noqa: E731
            if not vals or s[key] is None:
                rows.append(f"| {s['horizon']}d | {label} | {fmt(s[key])} | n/a | n/a | n/a |")
                continue
            sd = statistics.stdev(vals) if len(vals) > 1 else 0.0
            # share of random runs the strategy beats (ties count half); for
            # drawdown lower is better
            better = (lambda v: s[key] < v) if key == "max_drawdown" else (lambda v: s[key] > v)
            beat = sum(1.0 if better(v) else 0.5 if v == s[key] else 0.0 for v in vals) / len(vals)
            rows.append(f"| {s['horizon']}d | {label} | {fmt(s[key])} | "
                        f"{fmt(statistics.mean(vals))} +/- {sd * scale:.{digits}f} | "
                        f"{fmt(min(vals))} .. {fmt(max(vals))} | "
                        f"{'' if key == 'n' else f'{beat * 100:.0f}%'} |")
    return "\n".join(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    ap.add_argument("--history", type=Path, default=HISTORY)
    ap.add_argument("--start", help="first pick date, YYYY-MM-DD (default: one year into the file)")
    ap.add_argument("--end", help="last pick date, YYYY-MM-DD (default: last day in the file)")
    ap.add_argument("--top-n", type=int, default=10)
    ap.add_argument("--min-score", type=float, default=2.0, help="PV score threshold (max 3.0)")
    ap.add_argument("--bear-min-score", type=float, default=2.5,
                    help="threshold on bear-regime days (production: 4.0 -> 5.0)")
    ap.add_argument("--horizons", type=int, nargs="+", default=[5, 10, 20])
    ap.add_argument("--fill", choices=("open", "close"), default="open",
                    help="enter at the next open (default) or the pick day's close")
    ap.add_argument("--no-regime", action="store_true", help="ignore the SPY regime filter")
    ap.add_argument("--threads", type=int, default=0, help="0 = every hardware thread")
    ap.add_argument("--picks-csv", type=Path, help="also write every pick to this CSV")
    ap.add_argument("--baseline", choices=("random",),
                    help="also run a random-pick baseline from the same daily candidates")
    ap.add_argument("--seeds", type=int, default=20, help="random baseline runs (seeds 1..N)")
    args = ap.parse_args()
    if args.seeds < 2:
        ap.error("--seeds must be >= 2 to report a spread")

    if not args.history.exists():
        sys.exit(f"{args.history} not found: run python -m scripts.export_history")

    cfg = engine_config() | {
        "MIN_PRICE": config.MIN_PRICE,
        "MIN_AVG_VOLUME": config.MIN_AVG_VOLUME,
        "SECTOR_SLOPE_MIN": config.SECTOR_SLOPE_MIN,
    }
    start = yyyymmdd(args.start) if args.start else None
    if start is None:
        dates = read_history_bin(args.history)["dates"]
        start = int(dates[min(WARMUP_SESSIONS, len(dates) - 1)])

    def backtest(**selection) -> dict:
        return mscan_engine.run_backtest(
            str(args.history), cfg,
            start=start, end=yyyymmdd(args.end) if args.end else None,
            exclude=sorted(SECTOR_PEERS), spy=SPY,
            top_n=args.top_n, min_pv_score=args.min_score, bear_min_pv_score=args.bear_min_score,
            horizons=args.horizons,
            fill="open_next_day" if args.fill == "open" else "close_same_day",
            regime_filter=not args.no_regime, num_threads=args.threads, **selection,
        )

    result = backtest()

    fill = "next open" if args.fill == "open" else "same-day close"
    print(f"PV-only backtest {result['start']} .. {result['end']} "
          f"({result['num_days']} sessions, {result['num_tickers']:,} tickers in the file)")
    print(f"top {args.top_n}, score >= {args.min_score} "
          f"({args.bear_min_score} on {result['bear_days']} bear days"
          f"{', regime filter off' if args.no_regime else ''}), fill at {fill}")
    print(f"{len(result['picks']):,} picks from {result['evaluations']:,} ticker-days scored "
          f"in {result['elapsed_ms'] / 1000:.2f} s\n")
    print(stats_table(result["stats"]))

    if args.baseline == "random":
        runs = [backtest(selection="random", seed=seed)["stats"] for seed in range(1, args.seeds + 1)]
        uncontested = result["num_days"] - result["contested_days"]
        print(f"\nRandom baseline: each day, top {args.top_n} drawn uniformly from the same "
              f"candidates the strategy chooses from, seeds 1..{args.seeds}. "
              f"{result['contested_days']} of {result['num_days']} days have more than "
              f"{args.top_n} candidates"
              + (f"; on the other {uncontested} both pick the same set." if uncontested else ".")
              + " sd and range are across seeds; pctile is the share of seeds the strategy "
              "beats (lower is better for drawdown).\n")
        print(baseline_table(result["stats"], runs))

    if args.picks_csv:
        with args.picks_csv.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["date", "ticker", "score",
                        *(f"ret_{h}d" for h in args.horizons),
                        *(f"excess_{h}d" for h in args.horizons)])
            for p in result["picks"]:
                w.writerow([p["date"], p["ticker"], p["score"], *p["fwd_returns"], *p["excess_vs_spy"]])
        print(f"\nwrote {len(result['picks']):,} picks to {args.picks_csv}")


if __name__ == "__main__":
    main()
