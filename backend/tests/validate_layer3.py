"""
python -m backend.tests.validate_layer3

Validates Layer 3 options flow scoring for 6 tickers with liquid options markets.
Signal E is advisory on the first run (no SQLite history yet).
Signal F is advisory when Alpaca keys are not configured.
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

logging.basicConfig(level=logging.WARNING, format="%(levelname)s  %(message)s")

from backend.config import (
    LAYER3_MIN_SIGNALS,
    L3_SIG_A_VOL_CUTOFF_HIGH,
    L3_SIG_A_VOL_CUTOFF_LOW,
    L3_SIG_A_RATIO_MEGA,
    L3_SIG_A_RATIO_LARGE,
    L3_SIG_A_RATIO_MID,
)
from backend.data.fetcher_options import fetch_options_signals, fetch_vwap
from backend.data.fetcher_price import fetch_daily_ohlcv
from backend.scoring.layer3_options import score

# 6 tickers with large, liquid options markets -- consistent daily flow data
TEST_TICKERS = ["NVDA", "TSLA", "AAPL", "AMZN", "META", "MSFT"]


def _build_bundle(ticker: str) -> dict:
    opts    = fetch_options_signals(ticker)
    vwap    = fetch_vwap(ticker)
    ohlcv   = fetch_daily_ohlcv(ticker)
    last_px = float(ohlcv["Close"].iloc[-1]) if not ohlcv.empty else None
    return {
        "ticker":          ticker,
        "options_signals": opts,
        "last_price":      last_px,
        "vwap":            vwap,
    }


def _tf(v: bool) -> str:
    return "T" if v else "."


def main() -> None:
    print(f"\nFetching options signals + VWAP for {len(TEST_TICKERS)} tickers ...\n")

    bundles: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = {ex.submit(_build_bundle, t): t for t in TEST_TICKERS}
        for f in as_completed(futures):
            t = futures[f]
            bundles[t] = f.result()

    # ------------------------------------------------------------------
    # Score all tickers
    # ------------------------------------------------------------------
    results: dict[str, dict] = {}
    for ticker in TEST_TICKERS:
        results[ticker] = score(bundles[ticker])

    # ------------------------------------------------------------------
    # Table
    # ------------------------------------------------------------------
    FMT = "{:<6}  {:>4} {:>4} {:>4} {:>4}  {:>5}  {:>5}  {:>7}  {:>9}  {:>8}  {:>6}  {:>5}"
    header = FMT.format(
        "Ticker", "A", "D", "E", "F", "Fired", "Score",
        "CVOR", "IV Slope", "ADOV", "Thresh", "Tier",
    )
    sep = "-" * len(header)

    print(header)
    print(sep)

    for ticker in TEST_TICKERS:
        out = results[ticker]
        s   = out["signals"]

        cvor_s   = f"{s['call_vol_oi_ratio']:.2f}" if s.get("call_vol_oi_ratio") is not None else "N/A"
        iv_s     = f"{s['iv_slope']:+.4f}"          if s.get("iv_slope")          is not None else "N/A"
        adov_s   = f"{s.get('avg_daily_option_volume', 0):,}"
        thresh_s = f"{s.get('cvor_threshold', '?')}"
        tier_s   = s.get("tier", "?")

        print(FMT.format(
            ticker,
            _tf(s.get("A", False)),
            _tf(s.get("D", False)),
            _tf(s.get("E", False)),
            _tf(s.get("F", False)),
            s.get("fired", 0),
            f"{out['score']:.1f}",
            cvor_s,
            iv_s,
            adov_s,
            thresh_s,
            tier_s,
        ))

    print()

    # ------------------------------------------------------------------
    # Per-signal notes
    # ------------------------------------------------------------------
    print(sep)
    print(f"\nSignal A tiers  (adov>{L3_SIG_A_VOL_CUTOFF_HIGH:,}→{L3_SIG_A_RATIO_MEGA}  "
          f"adov>{L3_SIG_A_VOL_CUTOFF_LOW:,}→{L3_SIG_A_RATIO_LARGE}  "
          f"else→{L3_SIG_A_RATIO_MID}  |  need >={LAYER3_MIN_SIGNALS} signals for 1.0):\n")

    for ticker in TEST_TICKERS:
        s     = results[ticker]["signals"]
        notes = []

        tier_s   = s.get("tier", "?")
        thresh   = s.get("cvor_threshold", "?")
        adov_val = s.get("avg_daily_option_volume", 0)
        if s.get("A"):
            notes.append(
                f"A fired: CVOR={s['call_vol_oi_ratio']:.2f} > {thresh}  "
                f"[{tier_s} tier, adov={adov_val:,}]"
            )
        else:
            cv = s.get("call_vol_oi_ratio")
            if cv is not None:
                notes.append(
                    f"A miss: CVOR={cv:.2f} <= {thresh}  [{tier_s} tier, adov={adov_val:,}]"
                )
            else:
                notes.append("A miss: CVOR=N/A (no options data)")

        if s.get("D"):
            notes.append(f"D fired: IV slope {s['iv_slope']:+.4f} < 0 (contango)")
        else:
            iv = s.get("iv_slope")
            notes.append(f"D miss: IV slope={iv:+.4f}" if iv is not None else "D miss: IV slope=N/A")

        if s.get("E"):
            notes.append(f"E fired: PCR {s['pc_ratio']:.3f} < 5d avg {s['pc_ratio_5d_avg']:.3f}")
        elif s.get("pc_ratio_5d_avg") is None:
            notes.append("E n/a: no 5d history yet (first run -- rerun tomorrow)")
        else:
            notes.append(f"E miss: PCR {s.get('pc_ratio', 'N/A')} >= 5d avg {s['pc_ratio_5d_avg']:.3f}")

        if s.get("F"):
            notes.append(f"F fired: price {s['last_price']:.2f} > VWAP {s['vwap']:.2f}")
        elif s.get("vwap") is None:
            notes.append("F n/a: VWAP unavailable (Alpaca keys not set or market closed)")
        else:
            notes.append(f"F miss: price {s.get('last_price', 'N/A')} <= VWAP {s['vwap']:.2f}")

        print(f"  {ticker}:")
        for note in notes:
            print(f"    {note}")

    # ------------------------------------------------------------------
    # Advisory checks
    # ------------------------------------------------------------------
    print()
    print(sep)
    print("\nAdvisory:")
    any_data = any(results[t]["signals"].get("call_vol_oi_ratio") is not None for t in TEST_TICKERS)
    if not any_data:
        print("  NOTE: No options data retrieved -- check yfinance connectivity.")
    else:
        print("  OK  : Options data fetched for at least one ticker.")

    all_E_na = all(results[t]["signals"].get("pc_ratio_5d_avg") is None for t in TEST_TICKERS)
    if all_E_na:
        print("  NOTE: Signal E unavailable (first run, no SQLite history). "
              "Re-run on a subsequent trading day to populate 5d avg.")
    else:
        print("  OK  : SQLite history present; signal E active.")

    all_F_na = all(results[t]["signals"].get("vwap") is None for t in TEST_TICKERS)
    if all_F_na:
        print("  NOTE: Signal F unavailable (Alpaca keys not set or market closed).")
    else:
        print("  OK  : Intraday VWAP fetched from Alpaca.")

    print("\nLayer 3 validation complete.")


if __name__ == "__main__":
    main()
