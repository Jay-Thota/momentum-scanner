"""
backend/tests/validate_db.py

Validates the smart-fetch pipeline:
  - Alpaca batch OHLCV vs Yahoo OHLCV fallback counts
  - MetadataCache hit/miss stats (Run 1 cold, Run 2 hot)
  - Run 2 must have 0 Yahoo metadata calls
  - Ranked watchlist from DB

Run with:
    python -m backend.tests.validate_db
"""

import json
import logging
import sys
import time
from datetime import date

from sqlalchemy import func, select

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)

SEP      = "-" * 72
PASS_SYM = "[PASS]"
FAIL_SYM = "[FAIL]"


def _pf(cond: bool, label: str, detail: str = "") -> bool:
    sym  = PASS_SYM if cond else FAIL_SYM
    line = f"  {sym} {label}"
    if detail:
        line += f"  ({detail})"
    print(line)
    return cond


def _header(title: str) -> None:
    print(f"\n{SEP}")
    print(f"  {title}")
    print(SEP)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _show_watchlist_from_db(scan_date: str | None = None) -> list[dict]:
    from backend.db.database import SessionLocal, init_db
    from backend.db.models import ScanResult as ScanResultRow

    init_db()
    with SessionLocal() as session:
        if scan_date is None:
            scan_date = session.execute(
                select(func.max(ScanResultRow.scan_date))
            ).scalar_one_or_none()

        if scan_date is None:
            print("  No scan results in DB.")
            return []

        rows = session.execute(
            select(ScanResultRow)
            .where(ScanResultRow.scan_date == scan_date)
            .order_by(ScanResultRow.total_score.desc())
        ).scalars().all()

    if not rows:
        print(f"  No results for scan_date={scan_date}")
        return []

    print(f"  Scan date: {scan_date}  |  {len(rows)} rows")
    print(f"\n  {'Rank':<5} {'Ticker':<8} {'Total':>6} {'L1':>5} "
          f"{'L2':>5} {'L3':>5} {'L4':>5} {'L5':>5} {'Arch':<6} Regime")
    print(f"  {'-'*4} {'-'*7} {'-'*6} {'-'*5} {'-'*5} {'-'*5} {'-'*5} {'-'*5} {'-'*6} {'-'*6}")

    out = []
    for i, r in enumerate(rows, 1):
        print(
            f"  {i:<5} {r.ticker:<8} {r.total_score:>6.2f} "
            f"{(r.l1 or 0):>5.2f} {(r.l2 or 0):>5.2f} "
            f"{(r.l3 or 0):>5.2f} {(r.l4 or 0):>5.2f} {(r.l5 or 0):>5.2f} "
            f"{(r.archetype or '--'):<6} {(r.regime or '?')}"
        )
        out.append({"ticker": r.ticker, "total": r.total_score})
    return out


def _show_metadata_sample(n: int = 6) -> int:
    from backend.db.database import SessionLocal
    from backend.db.models import MetadataCache

    with SessionLocal() as session:
        rows = session.execute(select(MetadataCache).limit(n)).scalars().all()
        total = session.execute(select(func.count(MetadataCache.id))).scalar_one()

    if not rows:
        print("  MetadataCache table is empty.")
        return 0

    print(f"\n  {'Ticker':<8} {'Market Cap':>14} {'Sector':<26} {'ETF':>5} "
          f"{'SI%':>6} {'DTC':>5}  Last Updated")
    print(f"  {'-'*7} {'-'*14} {'-'*25} {'-'*5} {'-'*6} {'-'*5}  {'-'*19}")
    for r in rows:
        mc_s   = f"${(r.market_cap or 0)/1e9:.1f}B" if r.market_cap else "-"
        si_s   = f"{(r.si_pct or 0)*100:.1f}%" if r.si_pct is not None else "-"
        dtc_s  = f"{r.days_to_cover:.1f}" if r.days_to_cover is not None else "-"
        sec_s  = (r.sector or "-")[:24]
        ts_s   = (r.last_updated or "-")[:19]
        print(f"  {r.ticker:<8} {mc_s:>14} {sec_s:<26} {(r.etf_symbol or '-'):>5} "
              f"{si_s:>6} {dtc_s:>5}  {ts_s}")

    print(f"\n  Total cached rows: {total}")
    return total


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    _header("Smart-Fetch Pipeline Validation")

    from backend.db.database import init_db
    from backend.scoring.scorer import run_daily_scan
    init_db()

    all_pass = True

    # ── Run 1 (cold cache) ─────────────────────────────────────────────────
    _header("Run 1  --  cold MetadataCache expected")
    stats1: dict = {}
    t0 = time.perf_counter()
    watchlist1 = run_daily_scan(stats=stats1)
    elapsed1 = time.perf_counter() - t0

    print(f"\n  Elapsed: {elapsed1:.1f}s\n")
    col_w = 36
    for key in (
        "universe_size", "alpaca_ohlcv_count", "yahoo_ohlcv_fallback",
        "l0_pv_pass", "early_exit_discarded",
        "metadata_cache_hits", "metadata_yahoo_calls",
        "l0_pass", "scored", "qualified", "watchlist",
        "regime", "min_score", "elapsed_s",
    ):
        val = stats1.get(key, "-")
        print(f"  {key:<{col_w}} {str(val):>10}")

    print()
    all_pass &= _pf(
        stats1.get("alpaca_ohlcv_count", 0) > 400,
        "Alpaca returned OHLCV for >400 tickers",
        f"got {stats1.get('alpaca_ohlcv_count', 0)}",
    )
    all_pass &= _pf(
        stats1.get("yahoo_ohlcv_fallback", 999) < 150,
        "Yahoo OHLCV fallback < 150 tickers",
        f"got {stats1.get('yahoo_ohlcv_fallback', 0)}",
    )
    all_pass &= _pf(
        stats1.get("early_exit_discarded", -1) >= 0,
        "Early-exit counter present",
        f"discarded={stats1.get('early_exit_discarded', 0)}",
    )

    # ── MetadataCache sample ───────────────────────────────────────────────
    _header("MetadataCache sample (after Run 1)")
    total_cached = _show_metadata_sample(n=8)
    all_pass &= _pf(total_cached > 0, "MetadataCache populated", f"{total_cached} rows")

    # ── Watchlist from DB ─────────────────────────────────────────────────
    _header("Watchlist from DB (Run 1)")
    db_rows = _show_watchlist_from_db()
    all_pass &= _pf(len(db_rows) > 0, "At least one result saved to DB", f"{len(db_rows)} rows")

    in_mem  = {r.ticker for r in watchlist1}
    db_tick = {r["ticker"] for r in db_rows}
    missing = in_mem - db_tick
    all_pass &= _pf(not missing, "All in-memory tickers in DB", f"missing={missing or 'none'}")

    # ── Run 2 (hot cache) ─────────────────────────────────────────────────
    _header("Run 2  --  hot cache  (0 Yahoo metadata calls expected)")
    stats2: dict = {}
    t0 = time.perf_counter()
    watchlist2 = run_daily_scan(stats=stats2)
    elapsed2 = time.perf_counter() - t0

    hits2  = stats2.get("metadata_cache_hits",  0)
    yahoo2 = stats2.get("metadata_yahoo_calls", -1)
    print(f"\n  Elapsed (Run 2): {elapsed2:.1f}s")
    print(f"  metadata_cache_hits  (Run 2): {hits2}")
    print(f"  metadata_yahoo_calls (Run 2): {yahoo2}\n")

    all_pass &= _pf(
        yahoo2 == 0,
        "Run 2: metadata_yahoo_calls == 0  (all served from cache)",
        f"got {yahoo2}",
    )
    all_pass &= _pf(
        hits2 >= stats1.get("metadata_yahoo_calls", 0),
        "Run 2: cache hits >= Run-1 Yahoo calls",
        f"hits={hits2}",
    )

    # ── Final summary ─────────────────────────────────────────────────────
    _header("Summary")
    if all_pass:
        print(f"  {PASS_SYM} All checks passed.  Smart-fetch pipeline working correctly.\n")
    else:
        print(f"  {FAIL_SYM} Some checks failed -- see output above.\n")

    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
