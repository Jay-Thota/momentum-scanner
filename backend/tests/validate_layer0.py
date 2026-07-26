"""
python -m backend.tests.validate_layer0

Validates the universe fetch and Layer 0 filter.

  1. Prints universe size and exchange breakdown
  2. Confirms ticker count is in the 6,000-8,000 range
  3. Runs the Layer 0 price/volume/market_cap check on known pass/fail tickers
  4. Confirms all expected outcomes match
"""

import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import pandas as pd
import yfinance as yf

from backend.config import MIN_PRICE, MIN_AVG_VOLUME, MIN_MARKET_CAP
from backend.scoring.layer0_universe import check
from backend.utils.market_calendar import get_next_n_trading_days

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)

YAHOO_ALIAS: dict[str, str] = {"BRK.B": "BRK-B", "BF.B": "BF-B"}


# ---------------------------------------------------------------------------
# Data fetching helpers (unchanged from previous version)
# ---------------------------------------------------------------------------

def _extract_earnings_date(t: yf.Ticker) -> date | None:
    today = date.today()
    try:
        cal = t.calendar
        if not cal:
            return None
        raw_dates = []
        if isinstance(cal, dict):
            val = cal.get("Earnings Date", [])
            raw_dates = val if isinstance(val, list) else [val]
        for d in raw_dates:
            try:
                ts = pd.Timestamp(d)
                d_date = ts.tz_localize(None).date() if ts.tzinfo else ts.date()
                if d_date >= today:
                    return d_date
            except Exception:
                continue
    except Exception:
        pass
    return None


def fetch_bundle(ticker: str) -> dict:
    try:
        yahoo_sym = YAHOO_ALIAS.get(ticker, ticker)
        t = yf.Ticker(yahoo_sym)
        info = t.info
        hist = t.history(period="30d")
        price       = float(info.get("currentPrice") or info.get("regularMarketPrice") or 0.0)
        avg_volume  = float(hist["Volume"].tail(20).mean()) if not hist.empty else 0.0
        market_cap  = float(info.get("marketCap") or 0.0)
        earnings_date = _extract_earnings_date(t)
        return {"price": price, "avg_volume": avg_volume,
                "market_cap": market_cap, "earnings_date": earnings_date}
    except Exception as exc:
        logging.warning("fetch_bundle %s: %s", ticker, exc)
        return {"price": 0.0, "avg_volume": 0.0, "market_cap": 0.0, "earnings_date": None}


def find_cheap_stock() -> tuple[str, dict] | tuple[None, None]:
    from backend.universe import get_universe
    universe = set(get_universe())
    candidates = [c for c in ["F", "SIRI", "AAL", "SOFI", "NIO", "PLUG"] if c in universe]
    for ticker in candidates:
        bundle = fetch_bundle(ticker)
        if 0.0 < bundle["price"] < MIN_PRICE:
            return ticker, bundle
    return None, None


def find_earnings_stock(next_2: set[date]) -> tuple[str, dict] | tuple[None, None]:
    candidates = ["ORCL","ADBE","NKE","FDX","MU","LULU","ACN","KMX","PAYX","FDS","COST"]
    for ticker in candidates:
        bundle = fetch_bundle(ticker)
        ed = bundle.get("earnings_date")
        if ed is not None and ed in next_2:
            return ticker, bundle
    return None, None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    # ── Section 1: Universe size and exchange breakdown ────────────────────
    print("\n" + "=" * 64)
    print("  Universe validation")
    print("=" * 64)

    from backend.db.database import init_db, SessionLocal
    from backend.db.models import UniverseCache
    from sqlalchemy import func, select

    init_db()
    tickers = __import__("backend.universe", fromlist=["get_universe"]).get_universe()

    with SessionLocal() as session:
        exchange_counts = session.execute(
            select(UniverseCache.exchange, func.count().label("n"))
            .group_by(UniverseCache.exchange)
            .order_by(func.count().desc())
        ).all()

    print(f"\n  Total tickers in universe : {len(tickers):>7,}")
    for exc, cnt in exchange_counts:
        print(f"    {exc:<12}: {cnt:>7,}")

    # Sample to show the shape of data
    print(f"\n  First 10  : {tickers[:10]}")
    print(f"  Last  10  : {tickers[-10:]}")

    # Confirm expected range
    in_range = 6_000 <= len(tickers) <= 8_000
    range_sym = "[PASS]" if in_range else "[FAIL]"
    print(f"\n  {range_sym} Ticker count {len(tickers):,} {'is' if in_range else 'is NOT'} "
          f"in expected range 6,000-8,000")

    # ── Section 2: Layer 0 functional tests ────────────────────────────────
    print("\n" + "=" * 64)
    print("  Layer 0 functional tests")
    print("=" * 64)

    next_2_trading_days = get_next_n_trading_days(n=2)
    next_2_set = set(next_2_trading_days)

    print(f"\n  Next 2 trading days: {next_2_trading_days}")
    print(f"  Thresholds: price>=${MIN_PRICE:.2f}  "
          f"avg_vol>={MIN_AVG_VOLUME:,.0f}  "
          f"mkt_cap>=${MIN_MARKET_CAP:,.0f}\n")

    core_tickers = ["AAPL", "MSFT", "JPM", "BRK.B"]
    test_cases: list[tuple[str, bool]] = [
        ("AAPL",  True),
        ("MSFT",  True),
        ("JPM",   True),
        ("BRK.B", True),
    ]

    bundles: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(fetch_bundle, t): t for t in core_tickers}
        for f in as_completed(futures):
            bundles[futures[f]] = f.result()

    # Dynamic: cheap stock (expect FAIL on price)
    cheap_ticker, cheap_bundle = find_cheap_stock()
    if cheap_ticker:
        print(f"  Cheap stock found   : {cheap_ticker} @ ${cheap_bundle['price']:.2f}")
        bundles[cheap_ticker] = cheap_bundle
        test_cases.append((cheap_ticker, False))
    else:
        print("  No universe stock under $10 -- using synthetic override")
        synth = "SYNTH_LOW"
        bundles[synth] = {"price": 5.00, "avg_volume": 8_000_000.0,
                          "market_cap": 3_000_000_000.0, "earnings_date": None}
        test_cases.append((synth, False))

    # Dynamic: earnings gate (expect FAIL)
    earn_ticker, earn_bundle = find_earnings_stock(next_2_set)
    if earn_ticker:
        print(f"  Earnings stock found: {earn_ticker} reports {earn_bundle['earnings_date']}")
        bundles[earn_ticker] = earn_bundle
        test_cases.append((earn_ticker, False))
    else:
        print("  No earnings stock in window -- using synthetic override")
        synth_e = "SYNTH_EARN"
        bundles[synth_e] = {
            "price": 150.00, "avg_volume": 5_000_000.0,
            "market_cap": 50_000_000_000.0,
            "earnings_date": next_2_trading_days[0] if next_2_trading_days else date.today(),
        }
        test_cases.append((synth_e, False))

    # Print results table
    col = dict(ticker=20, price=9, avg_vol=15, mkt_cap=18, ec=14, expected=10, result=10, match=6)
    header = (
        f"  {'Ticker':<{col['ticker']}} {'Price':>{col['price']}} "
        f"{'AvgVol(20d)':>{col['avg_vol']}} {'MktCap':>{col['mkt_cap']}} "
        f"{'EarningsClear':>{col['ec']}} {'Expected':>{col['expected']}} "
        f"{'Result':>{col['result']}} {'Match':>{col['match']}}"
    )
    print(f"\n{header}")
    print("  " + "-" * (len(header) - 2))

    all_ok = in_range
    for ticker, expected_pass in test_cases:
        bundle = bundles[ticker]
        result = check(ticker, bundle)
        ed = bundle["earnings_date"]
        earnings_clear = (ed is None) or (ed not in next_2_set)
        price_s = f"${bundle['price']:.2f}"
        vol_s   = f"{bundle['avg_volume']:>14,.0f}"
        cap_s   = f"${bundle['market_cap']:>16,.0f}"
        ec_s    = str(earnings_clear)
        exp_s   = "PASS" if expected_pass else "FAIL"
        res_s   = "PASS" if result else "FAIL"
        ok      = result == expected_pass
        match_s = "OK" if ok else "MISMATCH"
        if not ok:
            all_ok = False
        print(
            f"  {ticker:<{col['ticker']}} {price_s:>{col['price']}} "
            f"{vol_s:>{col['avg_vol']}} {cap_s:>{col['mkt_cap']}} "
            f"{ec_s:>{col['ec']}} {exp_s:>{col['expected']}} "
            f"{res_s:>{col['result']}} {match_s:>{col['match']}}"
        )

    # ── Summary ────────────────────────────────────────────────────────────
    print("\n" + "=" * 64)
    if all_ok:
        print("  All assertions passed.")
    else:
        print("  ASSERTION FAILED -- see rows above.")
    print("=" * 64 + "\n")

    if not all_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
