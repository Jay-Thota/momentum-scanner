"""
python -m backend.tests.validate_layer1

Validates Layer 1 volume-surge scoring across three ticker groups.
Output is a visual table; sanity checks are advisory (no sys.exit on signal mix).
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

# Suppress yfinance noise; only WARNING+ visible unless -v is passed.
logging.basicConfig(level=logging.WARNING, format="%(levelname)s  %(message)s")

from backend.data.fetcher_price import fetch_daily_ohlcv, fetch_intraday_block_trades
from backend.scoring.layer1_volume import score
from backend.config import VOLUME_SURGE_RATIO, RVOL_THRESHOLD, UP_DOWN_VOL_RATIO_MIN

# ---------------------------------------------------------------------------
# 8-ticker test set
#   high_vol : names that frequently appear in top-volume lists -- expect A/B/D
#   quiet    : defensive large-caps with typically low daily variability
#   edge     : mid-volume industrials sitting closer to thresholds
# ---------------------------------------------------------------------------
TEST_GROUPS: dict[str, list[str]] = {
    "high_vol": ["NVDA", "TSLA", "AMD"],
    "quiet":    ["KO",   "JNJ",  "PG"],
    "edge":     ["HON",  "IBM"],
}
ALL_TICKERS: list[str] = [t for tickers in TEST_GROUPS.values() for t in tickers]


def _build_bundle(ticker: str) -> dict:
    ohlcv = fetch_daily_ohlcv(ticker)
    block = fetch_intraday_block_trades(ticker)
    return {"ticker": ticker, "ohlcv": ohlcv, "block_trades": block}


def _tf(v: bool) -> str:
    """T for True, dot for False -- compact signal column."""
    return "T" if v else "."


def main() -> None:
    print(f"\nFetching OHLCV for {len(ALL_TICKERS)} tickers ...\n")

    bundles: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(_build_bundle, t): t for t in ALL_TICKERS}
        for f in as_completed(futures):
            t = futures[f]
            bundles[t] = f.result()

    # ------------------------------------------------------------------
    # Table header
    # ------------------------------------------------------------------
    FMT = "{:<10} {:<5}  {:>8}  {:>6}  {:>4}{:>4}{:>4}{:>4}{:>4}  {:>5}"
    header = FMT.format(
        "Group", "Ticker",
        "VolRatio", "UD-Rat",
        "A", "B", "C", "D", "E",
        "Score",
    )
    sep = "-" * len(header)

    print(f"Thresholds:  A >= {VOLUME_SURGE_RATIO:.1f}x  B > {RVOL_THRESHOLD:.1f}x  "
          f"C (up/dn) > {UP_DOWN_VOL_RATIO_MIN:.1f}x  D OBV@20d-high  E block-trades")
    print(f"Score 1.0 when >= 2 signals fire.\n")
    print(header)
    print(sep)

    results: dict[str, dict] = {}
    for group, tickers in TEST_GROUPS.items():
        for ticker in tickers:
            bundle = bundles[ticker]
            out = score(bundle)
            results[ticker] = out
            s = out.get("signals", {})

            print(FMT.format(
                group,
                ticker,
                f"{s.get('vol_ratio', 0):.3f}",
                f"{s.get('up_down_ratio', 0):.3f}",
                _tf(s.get("A", False)),
                _tf(s.get("B", False)),
                _tf(s.get("C", False)),
                _tf(s.get("D", False)),
                _tf(s.get("E", False)),
                f"{out.get('score', 0):.1f}",
            ))
        print()

    # ------------------------------------------------------------------
    # Advisory sanity checks
    # ------------------------------------------------------------------
    print(sep)
    print("\nSanity checks (advisory -- volume patterns vary by day):")

    high_scores    = [results[t]["score"] for t in TEST_GROUPS["high_vol"]]
    quiet_scores   = [results[t]["score"] for t in TEST_GROUPS["quiet"]]

    if all(s == 0.0 for s in high_scores):
        print("  NOTE: All 3 high-vol tickers scored 0 -- check if today was an unusually quiet session.")
    else:
        nhigh = sum(s == 1.0 for s in high_scores)
        print(f"  OK  : {nhigh}/3 high-vol ticker(s) scored 1.0")

    if any(s == 1.0 for s in quiet_scores):
        flagged = [t for t in TEST_GROUPS["quiet"] if results[t]["score"] == 1.0]
        print(f"  NOTE: Quiet ticker(s) scored 1.0: {flagged} -- possible news spike; verify manually.")
    else:
        print("  OK  : All quiet tickers scored 0.0")

    # Signal D sanity -- at least one stock somewhere should have OBV at 20d high
    any_D = any(
        results[t].get("signals", {}).get("D", False)
        for t in ALL_TICKERS
        if results[t].get("signals")
    )
    if not any_D:
        print("  NOTE: No ticker has OBV at 20d high -- possible data issue; inspect manually.")
    else:
        print("  OK  : At least one ticker has OBV at 20d high (signal D)")

    print("\nLayer 1 validation complete.")


if __name__ == "__main__":
    main()
