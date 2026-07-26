"""
backend/tests/validate_universe.py

Validates the dynamic universe fetch:
  - Forces a full refresh from Alpaca with $5 price + 200K volume pre-filters
  - Prints total count and exchange breakdown
  - Prints filtered-out counts with reasons
  - Prints 10 sample tickers per exchange
  - Asserts total in 2,000-5,000 range
  - Asserts required tickers present (AAPL, MSFT, NVDA, JPM)
  - Asserts excluded/delisted tickers absent (HES, DFS, MRO)
  - Reports wall-clock time for the full refresh

Run with:
    python -m backend.tests.validate_universe
"""

import logging
import sys
import time

from sqlalchemy import select, func

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)

SEP  = "=" * 68
PASS = "[PASS]"
FAIL = "[FAIL]"


def _pf(cond: bool, label: str, detail: str = "") -> bool:
    sym  = PASS if cond else FAIL
    line = f"  {sym}  {label}"
    if detail:
        line += f"  ({detail})"
    print(line)
    return cond


def main() -> None:
    from backend.db.database import SessionLocal, init_db
    from backend.db.models import UniverseCache
    from backend.universe import get_universe

    init_db()

    print(f"\n{SEP}")
    print("  Universe validation  --  forced full refresh")
    print(SEP)

    t0 = time.perf_counter()
    refresh_stats: dict = {}

    with SessionLocal() as db:
        tickers = get_universe(db, force_refresh=True, stats=refresh_stats)
        elapsed = time.perf_counter() - t0

        # ── Exchange breakdown from cache ──────────────────────────────────
        rows_by_exchange = db.execute(
            select(UniverseCache.exchange, func.count().label("n"))
            .group_by(UniverseCache.exchange)
            .order_by(func.count().desc())
        ).all()

        # ── Sample tickers per exchange ────────────────────────────────────
        samples: dict[str, list[str]] = {}
        for exc, _ in rows_by_exchange:
            sample = db.execute(
                select(UniverseCache.ticker)
                .where(UniverseCache.exchange == exc)
                .order_by(UniverseCache.ticker)
                .limit(10)
            ).scalars().all()
            samples[exc] = list(sample)

    # ── Print summary ──────────────────────────────────────────────────────
    ticker_set = set(tickers)
    nyse_n     = refresh_stats.get("nyse_n", sum(1 for exc, _ in rows_by_exchange if "NYSE" in exc.upper()))
    nasd_n     = refresh_stats.get("nasd_n", sum(1 for exc, _ in rows_by_exchange if "NASDAQ" in exc.upper()))
    n_pre      = refresh_stats.get("n_pre_filter", 0)
    n_no_data  = refresh_stats.get("n_no_data",   0)
    n_price    = refresh_stats.get("n_low_price", 0)
    n_vol      = refresh_stats.get("n_low_vol",   0)

    print(f"\n  Total tickers in universe : {len(tickers):>7,}")
    print(f"    NYSE                  : {nyse_n:>7,}")
    print(f"    Nasdaq                : {nasd_n:>7,}")
    print(f"\n  Refresh elapsed           : {elapsed:>7.1f}s")

    if n_pre:
        n_passed  = n_pre - n_no_data - n_price - n_vol
        print(f"\n  Pre-filter funnel ({n_pre:,} candidates from Alpaca):")
        print(f"    Passed price+volume filters : {n_passed:>7,}")
        print(f"    Dropped -- no Alpaca data   : {n_no_data:>7,}")
        print(f"    Dropped -- price < $5       : {n_price:>7,}")
        print(f"    Dropped -- volume < 200K    : {n_vol:>7,}")

    for exc, syms in samples.items():
        print(f"\n  {exc} sample (first 10 alpha): {syms}")

    # ── Assertions ─────────────────────────────────────────────────────────
    print(f"\n{SEP}")
    print("  Assertions")
    print(SEP)

    all_pass = True

    all_pass &= _pf(
        2_000 <= len(tickers) <= 5_000,
        "Total ticker count in range 2,000-5,000",
        f"got {len(tickers):,}",
    )

    for must_have in ("AAPL", "MSFT", "NVDA", "JPM"):
        all_pass &= _pf(must_have in ticker_set, f"{must_have} is present")

    for must_not in ("HES", "DFS", "MRO"):
        all_pass &= _pf(must_not not in ticker_set, f"{must_not} is excluded (delisted/acquired)")

    if n_pre:
        all_pass &= _pf(
            n_price > 0 or n_vol > 0,
            "$5 price and 200K volume filters were applied during construction",
            f"price_dropped={n_price}  vol_dropped={n_vol}",
        )

    all_pass &= _pf(
        elapsed < 900,
        "Full refresh completed in under 15 minutes",
        f"{elapsed:.1f}s",
    )

    print(f"\n{SEP}")
    if all_pass:
        print(f"  {PASS}  All assertions passed.\n")
    else:
        print(f"  {FAIL}  Some assertions failed -- see above.\n")

    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
