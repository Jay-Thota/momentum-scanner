"""
python -m backend.tests.validate_layer4

Validates Layer 4 short-interest gate for 8 tickers.

Expected hard-disqualifications:
  GME  -- si_pct ~14%  AND days_to_cover ~5.5  (both gates)
  RIVN -- si_pct ~14%  AND days_to_cover ~5.2  (both gates)
  SMCI -- si_pct ~18%  (SI gate alone; DTC ~2.2 is under threshold)

Expected passes with bonus:
  AAPL, MSFT -- clean mega-caps; minimal short interest
  TSLA       -- low SI despite volatility
  NVDA       -- low SI; strong growth expected => higher bonus
  JNJ        -- low SI; stable large-cap
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

from backend.config import (
    DAYS_TO_COVER_MAX,
    REVENUE_GROWTH_MIN,
    SHORT_INTEREST_MAX_PCT,
)
from backend.data.fetcher_short import (
    download_finra_ssho,
    get_short_data_yfinance,
    get_short_trend,
)
from backend.scoring.layer4_short import score

# Tickers: 3 hard-disqualify (GME, RIVN, SMCI) + 5 passes
HARD_DISQUALIFY = {"GME", "RIVN", "SMCI"}
TEST_TICKERS = ["GME", "RIVN", "SMCI", "TSLA", "AAPL", "MSFT", "NVDA", "JNJ"]


def _fetch_si(ticker: str) -> tuple[str, dict]:
    return ticker, get_short_data_yfinance(ticker)


def main() -> None:
    print(f"\nLayer 4 validation -- {len(TEST_TICKERS)} tickers")
    print(f"Gates: SI >= {SHORT_INTEREST_MAX_PCT:.0f}%  |  DTC >= {DAYS_TO_COVER_MAX:.1f}  |  trend==rising\n")

    # -- Download FINRA files (current + ~10 trading-day-slots older) --
    print("Downloading FINRA SSHO files ...")
    current_df = download_finra_ssho(offset_days=0)
    prev_df    = download_finra_ssho(offset_days=10)

    if current_df.empty:
        print("  NOTE: Current FINRA file unavailable -- trends will be 'flat'\n")
    else:
        print(f"  Current FINRA: {len(current_df):,} symbols")
    if prev_df.empty:
        print("  NOTE: Previous FINRA file unavailable -- trends will be 'flat'\n")
    else:
        print(f"  Previous FINRA: {len(prev_df):,} symbols\n")

    # -- Fetch yfinance SI data in parallel --
    print("Fetching yfinance short interest ...")
    si_data: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(_fetch_si, t): t for t in TEST_TICKERS}
        for f in as_completed(futures):
            ticker, data = f.result()
            si_data[ticker] = data

    # -- Build bundles and score --
    results: dict[str, tuple[dict, float | None]] = {}
    for ticker in TEST_TICKERS:
        si  = si_data[ticker]
        trend = get_short_trend(ticker, current_df, prev_df)
        bundle = {
            "ticker":        ticker,
            "si_pct":        si.get("si_pct"),
            "days_to_cover": si.get("days_to_cover"),
            "si_trend":      trend,
        }
        result = score(bundle)
        results[ticker] = (bundle, result)

    # -- Table --
    print()
    HDR = "{:<6}  {:>7}  {:>7}  {:>9}  {:>9}  {:>8}  {:>6}"
    print(HDR.format("Ticker", "SI%", "DTC", "Trend", "HardPass", "Bonus", "SCORE"))
    print("-" * 62)

    for ticker in TEST_TICKERS:
        bundle, result = results[ticker]
        si_pct = bundle.get("si_pct")
        dtc    = bundle.get("days_to_cover")
        trend  = bundle.get("si_trend", "flat")

        si_s     = f"{si_pct*100:.1f}%"  if si_pct is not None else "N/A"
        dtc_s    = f"{dtc:.2f}"          if dtc    is not None else "N/A"
        pass_s   = "PASS" if result is not None else "FAIL"
        bonus_s  = f"{result:.2f}" if result is not None else "--"
        score_s  = bonus_s  # Layer 4 score IS the bonus (or None)

        print(HDR.format(ticker, si_s, dtc_s, trend, pass_s, bonus_s, score_s))

    print()

    # -- Assertions --
    print("Assertions:")
    failures: list[str] = []

    for ticker in HARD_DISQUALIFY:
        _, result = results[ticker]
        if result is not None:
            msg = f"  FAIL  {ticker} expected None (hard-disqualify) but got {result}"
            print(msg)
            failures.append(msg)
        else:
            print(f"  OK    {ticker} correctly returned None (hard-disqualified)")

    for ticker in TEST_TICKERS:
        if ticker in HARD_DISQUALIFY:
            continue
        _, result = results[ticker]
        if result is None:
            msg = f"  WARN  {ticker} hard-disqualified (check current SI data -- may have changed)"
            print(msg)
        else:
            print(f"  OK    {ticker} passed gate with bonus={result:.2f}")

    print()
    if failures:
        print(f"VALIDATION FAILED: {len(failures)} hard-disqualify assertion(s) did not hold.")
        print("  Re-check yfinance SI data -- shortPercentOfFloat or shortRatio may have changed.")
    else:
        print("Layer 4 validation complete.")


if __name__ == "__main__":
    main()
