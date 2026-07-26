"""
python -m backend.tests.validate_layer5

Validates Layer 5 sector & macro alignment for 5 tickers across different sectors.
Prints current regime status first, then the per-ticker scoring table.
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

from backend.config import RS_RANK_THRESHOLD
from backend.data.fetcher_sector import (
    build_sector_returns,
    compute_sector_rs_rank,
    fetch_sector_data,
)
from backend.scoring.layer5_sector import score
from backend.utils.regime_filter import get_regime

# 5 tickers spanning 5 different sectors
TEST_TICKERS: list[tuple[str, str]] = [
    ("AAPL", "XLK"),   # Technology
    ("JPM",  "XLF"),   # Financials
    ("XOM",  "XLE"),   # Energy
    ("JNJ",  "XLV"),   # Health Care
    ("AMZN", "XLY"),   # Consumer Discretionary
]


def _tf(v: bool | None) -> str:
    if v is None: return "?"
    return "T" if v else "."


def main() -> None:
    # ------------------------------------------------------------------
    # Regime
    # ------------------------------------------------------------------
    print("\nFetching market regime (SPX 200-day MA) ...")
    regime = get_regime()

    slope_s = (f"{regime['ma_slope']:+.4%}" if regime["ma_slope"] is not None else "N/A")
    print(f"  Regime   : {regime['regime'].upper()}")
    print(f"  SPX      : {regime['spx_price']}")
    print(f"  200MA    : {regime['spx_200ma']}")
    print(f"  Above MA : {regime['above_200ma']}")
    print(f"  MA slope : {slope_s}")
    print()

    # ------------------------------------------------------------------
    # Fetch sector ETF data + sector returns in parallel
    # ------------------------------------------------------------------
    unique_etfs = list({etf for _, etf in TEST_TICKERS})
    print(f"Fetching sector ETF data + RS returns for: {', '.join(unique_etfs)} ...")

    etf_data_map:      dict[str, dict]          = {}
    sector_returns_map: dict[str, dict[str, float]] = {}

    with ThreadPoolExecutor(max_workers=15) as ex:
        all_futures: dict = {}
        for etf in unique_etfs:
            all_futures[ex.submit(fetch_sector_data,    etf)] = ("etf", etf)
            all_futures[ex.submit(build_sector_returns, etf)] = ("sr",  etf)

        for f in as_completed(all_futures):
            kind, etf = all_futures[f]
            if kind == "etf":
                etf_data_map[etf]      = f.result()
            else:
                sector_returns_map[etf] = f.result()

    print()

    # ------------------------------------------------------------------
    # Score
    # ------------------------------------------------------------------
    results: dict[str, dict] = {}
    for ticker, etf in TEST_TICKERS:
        rs_rank = compute_sector_rs_rank(ticker, sector_returns_map.get(etf, {}))
        bundle = {
            "ticker":     ticker,
            "sector_etf": etf,
            "etf_data":   etf_data_map.get(etf),
            "rs_rank":    rs_rank,
        }
        results[ticker] = score(bundle, regime)

    # ------------------------------------------------------------------
    # Table
    # ------------------------------------------------------------------
    HDR = "{:<6}  {:>4}  {:>8}  {:>6}  {:>7}  {:>7}  {:>5}"
    print(HDR.format("Ticker", "ETF", "ETF>50MA", "Bull", "RS Rank", "Macro", "SCORE"))
    print("-" * 52)

    for ticker, etf in TEST_TICKERS:
        out = results[ticker]
        s   = out["signals"]

        rank_s  = f"#{s['rs_rank']}" if s.get("rs_rank") is not None else "N/A"
        macro_s = "T" if s.get("macro_clear") else "X(event)"

        print(HDR.format(
            ticker,
            etf,
            _tf(s.get("etf_above_50ma")),
            _tf(s.get("bull_regime")),
            rank_s,
            macro_s,
            f"{out['score']:.2f}",
        ))

    print()

    # ------------------------------------------------------------------
    # ETF detail
    # ------------------------------------------------------------------
    print("Sector ETF detail:")
    for etf in unique_etfs:
        d = etf_data_map.get(etf, {})
        if not d:
            print(f"  {etf}: no data")
            continue
        slope_s = f"{d['slope']:+.4%}" if d.get("slope") is not None else "N/A"
        trend   = "UP" if d.get("trending_up") else "DOWN"
        print(
            f"  {etf}: price={d.get('price','N/A'):>7}  50MA={d.get('ma50','N/A'):>7}"
            f"  slope={slope_s:>9}  above50={_tf(d.get('above_50ma'))}  trend={trend}"
        )

    print()

    # ------------------------------------------------------------------
    # RS detail
    # ------------------------------------------------------------------
    print(f"Relative-strength ranks  (top-{RS_RANK_THRESHOLD} = signal fires):")
    for ticker, etf in TEST_TICKERS:
        returns = sector_returns_map.get(etf, {})
        rank = compute_sector_rs_rank(ticker, returns)
        if returns and ticker in returns:
            ret_s = f"{returns[ticker]:+.1%}"
            print(f"  {ticker}: rank #{rank} of {len(returns)} peers  ({ret_s} 63d return)")
        else:
            print(f"  {ticker}: return not available for RS ranking")

    print()
    print("Layer 5 validation complete.")


if __name__ == "__main__":
    main()
