"""
python -m backend.tests.validate_layer2

Validates Layer 2 price-structure scoring across 6 representative tickers.
Covers all 4 archetypes where market conditions allow; advisory notes flag gaps.
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

logging.basicConfig(level=logging.WARNING, format="%(levelname)s  %(message)s")

from backend.data.fetcher_price import fetch_daily_ohlcv
from backend.scoring.layer2_structure import score

# ---------------------------------------------------------------------------
# Test set -- 6 tickers across 4 sectors, each with its sector ETF
# Archetype targets are aspirational; actual classification depends on the day.
#   NVDA / AAPL / META  --> XLK  (tech uptrend candidates  => arch 1)
#   XOM                 --> XLE  (energy rotation candidate => arch 4)
#   BAC                 --> XLF  (financials               => arch 4 or none)
#   TSLA                --> XLY  (consumer disc / volatile  => arch 2 or 3)
# ---------------------------------------------------------------------------
TICKER_ETF: dict[str, str] = {
    "NVDA": "XLK",
    "AAPL": "XLK",
    "META": "XLK",
    "XOM":  "XLE",
    "BAC":  "XLF",
    "TSLA": "XLY",
}
ALL_TICKERS = list(TICKER_ETF.keys())
ALL_ETFS    = list(set(TICKER_ETF.values()))


def _rs_20d(df: pd.DataFrame) -> float | None:
    """20-day return as a simple RS proxy."""
    if df.empty or len(df) < 21:
        return None
    return float(df["Close"].iloc[-1] / df["Close"].iloc[-21] - 1)


def _rank_rs(tickers: list[str], ohlcv_map: dict) -> dict[str, int]:
    """Rank tickers by 20d RS within a group; 1 = highest RS."""
    scores = {t: _rs_20d(ohlcv_map.get(t, pd.DataFrame())) for t in tickers}
    valid  = {t: v for t, v in scores.items() if v is not None}
    ranked = sorted(valid, key=lambda t: valid[t], reverse=True)
    return {t: ranked.index(t) + 1 for t in ranked}


def main() -> None:
    print(f"\nFetching OHLCV for {len(ALL_TICKERS)} tickers + {len(ALL_ETFS)} ETFs ...\n")

    all_syms = ALL_TICKERS + ALL_ETFS
    ohlcv_map: dict[str, pd.DataFrame] = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(fetch_daily_ohlcv, s): s for s in all_syms}
        for f in as_completed(futures):
            sym = futures[f]
            ohlcv_map[sym] = f.result()

    # Compute RS ranks within each sector group
    etf_to_tickers: dict[str, list[str]] = {}
    for t, etf in TICKER_ETF.items():
        etf_to_tickers.setdefault(etf, []).append(t)

    rs_ranks: dict[str, int] = {}
    for etf, members in etf_to_tickers.items():
        ranks = _rank_rs(members, ohlcv_map)
        rs_ranks.update(ranks)

    # ------------------------------------------------------------------
    # Score and collect results
    # ------------------------------------------------------------------
    results: dict[str, dict] = {}
    for ticker in ALL_TICKERS:
        bundle = {
            "ticker":           ticker,
            "ohlcv":            ohlcv_map.get(ticker, pd.DataFrame()),
            "sector_etf_ohlcv": ohlcv_map.get(TICKER_ETF[ticker], pd.DataFrame()),
            "sector_rs_rank":   rs_ranks.get(ticker),
        }
        results[ticker] = score(bundle)

    # ------------------------------------------------------------------
    # Print table
    # ------------------------------------------------------------------
    FMT = "{:<6} {:<4} {:>6}  {:>4}  {:>4}  {:>5}  {:>5}  {:>7}  {:>5}"
    header = FMT.format(
        "Ticker", "ETF",
        "Arch", "AScr",
        "A", "B", "C_pt",
        "MomScr", "TOTAL",
    )
    sep = "-" * len(header)
    print(header)
    print(sep)

    arch_seen: set[int] = set()
    for ticker in ALL_TICKERS:
        r   = results[ticker]
        s   = r.get("signals", {})
        etf = TICKER_ETF[ticker]
        arch = r["archetype"]
        if arch:
            arch_seen.add(arch)

        def _tf(v: bool) -> str:
            return "T" if v else "."

        print(FMT.format(
            ticker,
            etf,
            str(arch) if arch else "-",
            f"{r['archetype_score']:.1f}",
            _tf(s.get("A", False)),
            _tf(s.get("B", False)),
            f"{s.get('C_pts', 0):.2f}",
            f"{r['momentum_score']:.2f}",
            f"{r['total']:.2f}",
        ))

    print()

    # ------------------------------------------------------------------
    # Advisory checks
    # ------------------------------------------------------------------
    print(sep)
    print("\nAdvisory:")
    missing = set(range(1, 5)) - arch_seen
    if not missing:
        print("  OK  : All 4 archetypes represented in output.")
    else:
        print(f"  NOTE: Archetypes not seen today: {sorted(missing)} -- normal if market doesn't fit those patterns.")

    any_score = any(r["total"] > 0 for r in results.values())
    if not any_score:
        print("  NOTE: All totals are 0 -- possible data issue or very quiet/flat market session.")
    else:
        best = max(results, key=lambda t: results[t]["total"])
        print(f"  OK  : Highest-scoring ticker: {best}  total={results[best]['total']:.2f}")

    print("\nLayer 2 validation complete.")


if __name__ == "__main__":
    main()
