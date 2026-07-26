"""
python -m backend.tests.validate_full_scan

Runs the full daily scoring pipeline end-to-end.
Prints ranked watchlist table + scan statistics.
Saves results to scan_results/YYYY-MM-DD.json.

Target: completes in under 5 minutes.
"""

import json
import logging
import time
from datetime import date
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)

from backend.scoring.scorer import ScoringResult, run_daily_scan

_RESULTS_DIR = Path("scan_results")


def _save_results(results: list[ScoringResult], stats: dict) -> Path:
    _RESULTS_DIR.mkdir(exist_ok=True)
    today    = date.today().isoformat()
    out_path = _RESULTS_DIR / f"{today}.json"

    payload = {
        "date":      today,
        "stats":     stats,
        "watchlist": [r.to_dict() for r in results],
    }
    out_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return out_path


def main() -> None:
    print("\n" + "=" * 60)
    print("  Momentum Scanner -- Full Daily Scan")
    print("=" * 60 + "\n")

    wall_start = time.perf_counter()
    stats: dict = {}
    results = run_daily_scan(stats=stats)
    wall_elapsed = time.perf_counter() - wall_start

    # ------------------------------------------------------------------
    # Ranked table
    # ------------------------------------------------------------------
    print()
    print("=" * 60)
    print(f"  Top-{len(results)} Watchlist  "
          f"(regime: {stats.get('regime','?').upper()}  "
          f"threshold: {stats.get('min_score','?'):.1f})")
    print("=" * 60)

    HDR = "{:>4}  {:<6}  {:>5}  {:>4}  {:>4}  {:>4}  {:>4}  {:>4}  {}"
    print(HDR.format("Rank", "Ticker", "Total", " L1", " L2", " L3", " L4", " L5", "Archetype"))
    print("-" * 60)

    for i, r in enumerate(results, 1):
        arch = r.archetype or "--"
        print(HDR.format(
            i, r.ticker,
            f"{r.total:.2f}",
            f"{r.l1:.2f}", f"{r.l2:.2f}", f"{r.l3:.2f}",
            f"{r.l4:.2f}", f"{r.l5:.2f}",
            arch,
        ))

    if not results:
        print("  (no stocks met the minimum score threshold)")

    print()

    # ------------------------------------------------------------------
    # Scan statistics
    # ------------------------------------------------------------------
    print("-" * 60)
    print("Scan statistics:")
    print(f"  Universe        : {stats.get('universe_size','?')} tickers")
    print(f"  L0 pass         : {stats.get('l0_pass','?')}")
    print(f"  L4 disqualified : {stats.get('l4_fail','?')}")
    print(f"  Scored          : {stats.get('scored','?')}")
    print(f"  Qualified       : {stats.get('qualified','?')} (score >= {stats.get('min_score','?'):.1f})")
    print(f"  Errors          : {stats.get('errors',0)}")
    print(f"  Elapsed         : {wall_elapsed:.1f}s", end="")

    target_s = 300
    if wall_elapsed > target_s:
        print(f"  WARNING: exceeded {target_s}s target")
    else:
        print(f"  (target < {target_s}s  OK)")

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------
    out_path = _save_results(results, stats)
    print(f"\nResults saved: {out_path}")
    print()


if __name__ == "__main__":
    main()
