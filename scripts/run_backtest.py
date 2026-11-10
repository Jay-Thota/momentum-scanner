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

--baseline random also runs two random baselines --seeds times each, with
top_n picks a day drawn at random from (a) every ticker that passes the L0
gate, and (b) the strategy's own candidates (L0 gate + regime-adjusted score
threshold). It prints the strategy next to each baseline's mean, standard
deviation and range across seeds: (a) vs (b) tests the threshold, (b) vs the
strategy tests the ranking.

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
from backend.data.security_types import etf_symbols
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


def _values(runs: list[list[dict]], hi: int, key: str) -> list[float]:
    return [r[hi][key] for r in runs if r[hi][key] is not None]


def _beats(a: list[float], b: list[float], key: str) -> float:
    """Share of (a, b) pairs where a is better (ties half); lower drawdown is better."""
    better = (lambda x, y: x < y) if key == "max_drawdown" else (lambda x, y: x > y)
    return sum(1.0 if better(x, y) else 0.5 if x == y else 0.0 for x in a for y in b) / (len(a) * len(b))


def comparison_tables(rows: dict[str, list[list[dict]]]) -> str:
    """
    rows: label -> one stats list per run (a single run for a deterministic
    selection, one per seed for a random one). Prints mean +/- sample sd
    across runs, the min .. max range, and how often each row beats the ones
    above it.
    """
    labels = list(rows)
    horizons = [s["horizon"] for s in next(iter(rows.values()))[0]]
    fmt = lambda x, scale, digits: f"{x * scale:.{digits}f}"   # noqa: E731

    def cell(runs: list[list[dict]], hi: int, key: str, scale: int, digits: int) -> str:
        vals = _values(runs, hi, key)
        if not vals:
            return "n/a"
        if len(vals) == 1:
            return fmt(vals[0], scale, digits)
        return f"{fmt(statistics.mean(vals), scale, digits)} +/- {fmt(statistics.stdev(vals), scale, digits)}"

    out = ["| horizon | selection | " + " | ".join(f[1] for f in BASELINE_FIELDS) + " |",
           "|---|---|" + "---|" * len(BASELINE_FIELDS)]
    for hi, h in enumerate(horizons):
        for label in labels:
            out.append(f"| {h}d | {label} | "
                       + " | ".join(cell(rows[label], hi, key, scale, digits)
                                    for key, _, scale, digits in BASELINE_FIELDS)
                       + " |")

    out += ["", "Range across seeds (min .. max):", "",
            "| horizon | selection | " + " | ".join(f[1] for f in BASELINE_FIELDS[:-1]) + " |",
            "|---|---|" + "---|" * (len(BASELINE_FIELDS) - 1)]
    for hi, h in enumerate(horizons):
        for label in labels:
            if len(rows[label]) < 2:
                continue
            cells = []
            for key, _, scale, digits in BASELINE_FIELDS[:-1]:
                vals = _values(rows[label], hi, key)
                cells.append(f"{fmt(min(vals), scale, digits)} .. {fmt(max(vals), scale, digits)}"
                             if vals else "n/a")
            out.append(f"| {h}d | {label} | " + " | ".join(cells) + " |")

    pairs = [(a, b) for i, a in enumerate(labels) for b in labels[:i]]
    out += ["", "Share of run pairs where the first beats the second (50% = no difference; "
            "lower drawdown counts as better):", "",
            "| horizon | comparison | " + " | ".join(f[1] for f in BASELINE_FIELDS[:-1]) + " |",
            "|---|---|" + "---|" * (len(BASELINE_FIELDS) - 1)]
    for hi, h in enumerate(horizons):
        for a, b in pairs:
            cells = []
            for key, *_ in BASELINE_FIELDS[:-1]:
                va, vb = _values(rows[a], hi, key), _values(rows[b], hi, key)
                cells.append(f"{_beats(va, vb, key) * 100:.0f}%" if va and vb else "n/a")
            out.append(f"| {h}d | {a} vs {b} | " + " | ".join(cells) + " |")
    return "\n".join(out)


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
                    help="also run random-pick baselines from the L0 universe and the "
                         "strategy's candidates")
    ap.add_argument("--seeds", type=int, default=20, help="random baseline runs (seeds 1..N)")
    ap.add_argument("--tie-break", choices=("ticker", "signals"), default="ticker",
                    help="order of equal scores: ticker (alphabetical, like the scanner) or "
                         "layer 1 signals fired, then vol_ratio")
    ap.add_argument("--include-etfs", action="store_true",
                    help="keep exchange-flagged ETFs pickable (the live universe drops them)")
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
    history = read_history_bin(args.history)
    start = yyyymmdd(args.start) if args.start else None
    if start is None:
        dates = history["dates"]
        start = int(dates[min(WARMUP_SESSIONS, len(dates) - 1)])

    # Same rule as the live universe (backend/universe.py): no exchange-flagged
    # ETFs. SPY stays in the file as the benchmark; it is never pickable.
    symbols = {str(s) for s in history["symbols"]}
    etfs = set() if args.include_etfs else (etf_symbols() & symbols) - set(SECTOR_PEERS) - {SPY}
    exclude = sorted(set(SECTOR_PEERS) | etfs)

    def backtest(**selection) -> dict:
        return mscan_engine.run_backtest(
            str(args.history), cfg,
            start=start, end=yyyymmdd(args.end) if args.end else None,
            exclude=exclude, spy=SPY,
            top_n=args.top_n, min_pv_score=args.min_score, bear_min_pv_score=args.bear_min_score,
            horizons=args.horizons,
            fill="open_next_day" if args.fill == "open" else "close_same_day",
            regime_filter=not args.no_regime, num_threads=args.threads, **selection,
        )

    tie_break = "signals_then_volume" if args.tie_break == "signals" else "ticker"
    result = backtest(tie_break=tie_break)

    fill = "next open" if args.fill == "open" else "same-day close"
    print(f"PV-only backtest {result['start']} .. {result['end']} "
          f"({result['num_days']} sessions, {result['num_tickers']:,} tickers in the file; "
          f"not pickable: SPY, {len(SECTOR_PEERS)} sector ETFs"
          + (", other ETFs kept" if args.include_etfs else
             f", {len(etfs)} other exchange-flagged ETFs") + ")")
    print(f"top {args.top_n}, score >= {args.min_score} "
          f"({args.bear_min_score} on {result['bear_days']} bear days"
          f"{', regime filter off' if args.no_regime else ''}), fill at {fill}, "
          f"ties by {'L1 signals, then vol_ratio' if args.tie_break == 'signals' else 'ticker'}")
    print(f"{len(result['picks']):,} picks from {result['evaluations']:,} ticker-days scored "
          f"in {result['elapsed_ms'] / 1000:.2f} s\n")
    print(stats_table(result["stats"]))

    if args.baseline == "random":
        seeds = range(1, args.seeds + 1)
        rows = {
            "universe-random": [backtest(selection="random_universe", seed=s)["stats"] for s in seeds],
            "pool-random": [backtest(selection="random", seed=s)["stats"] for s in seeds],
        }
        if args.tie_break != "ticker":   # the scanner's own tie order, for reference
            rows["strategy, ticker ties"] = [backtest()["stats"]]
        rows["strategy" if args.tie_break == "ticker" else f"strategy, {args.tie_break} ties"] = \
            [result["stats"]]
        uncontested = result["num_days"] - result["contested_days"]
        print(f"\nRandom baselines, seeds 1..{args.seeds}, top {args.top_n} a day drawn uniformly:\n"
              f"  universe-random: from every eligible ticker that passes the L0 gate that day "
              f"(no score threshold, so the regime has no effect)\n"
              f"  pool-random: from the candidates the strategy chooses from (L0 gate + score "
              f"threshold, regime-adjusted). {result['contested_days']} of {result['num_days']} "
              f"days have more than {args.top_n}"
              + (f"; on the other {uncontested} it picks the strategy's set." if uncontested else ".")
              + "\nsd and range are across seeds.\n")
        print(comparison_tables(rows))

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
