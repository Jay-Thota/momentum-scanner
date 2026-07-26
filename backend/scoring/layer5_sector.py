"""
Layer 5: Sector & Macro Alignment.
Max 1.5 pts.

  0.50 pts -- sector ETF above 50-day MA and trending up
  0.50 pts -- bull market regime (SPX above 200-day MA + slope >= 0)
  0.25 pts -- top-RS_RANK_THRESHOLD relative strength in sector (63-day return)
  0.25 pts -- no macro headwind today (not a FOMC decision or CPI release date)

Macro headwind dates cover FOMC announcement days and BLS CPI release dates
for 2025-2026. Update this list annually.
"""

import logging
from datetime import date

from backend import config

logger = logging.getLogger(__name__)

# FOMC decision (announcement) days + BLS CPI release dates for 2025-2026.
# On these days the signal is suppressed (too much event risk).
_MACRO_HEADWIND_DATES: frozenset[date] = frozenset({
    # 2025 FOMC announcements
    date(2025,  1, 29), date(2025,  3, 19), date(2025,  5,  7), date(2025,  6, 18),
    date(2025,  7, 30), date(2025,  9, 17), date(2025, 10, 29), date(2025, 12, 10),
    # 2026 FOMC announcements
    date(2026,  1, 28), date(2026,  3, 18), date(2026,  5,  6), date(2026,  6, 17),
    date(2026,  7, 29), date(2026,  9, 16), date(2026, 10, 28), date(2026, 12,  9),
    # 2025 CPI releases (BLS 8:30 AM ET)
    date(2025,  1, 15), date(2025,  2, 12), date(2025,  3, 12), date(2025,  4, 10),
    date(2025,  5, 13), date(2025,  6, 11), date(2025,  7, 15), date(2025,  8, 12),
    date(2025,  9, 10), date(2025, 10, 14), date(2025, 11, 12), date(2025, 12, 10),
    # 2026 CPI releases
    date(2026,  1, 14), date(2026,  2, 11), date(2026,  3, 11), date(2026,  4,  9),
    date(2026,  5, 13), date(2026,  6, 10), date(2026,  7, 15), date(2026,  8, 12),
    date(2026,  9,  9), date(2026, 10, 14), date(2026, 11, 12), date(2026, 12,  9),
})


def score(data_bundle: dict, regime: dict) -> dict:
    """
    Layer 5 scorer.

    data_bundle keys:
        ticker      str
        sector_etf  str | None    -- SPDR ETF symbol, from get_stock_sector_etf()
        etf_data    dict | None   -- from fetch_sector_data()
        rs_rank     int | None    -- 1=best; from compute_sector_rs_rank()

    regime -- dict from get_regime()

    Returns:
        {
          "score":   float (0.0 to 1.5),
          "signals": {
              "sector_aligned": bool,
              "bull_regime":    bool,
              "top5_rs":        bool,
              "macro_clear":    bool,
              "rs_rank":        int | None,
              "regime":         str,
              "sector_etf":     str | None,
              "etf_above_50ma": bool | None,
              "etf_slope":      float | None,
          }
        }
    """
    ticker   = data_bundle.get("ticker", "?")
    etf      = data_bundle.get("sector_etf")
    etf_data = data_bundle.get("etf_data") or {}
    rs_rank  = data_bundle.get("rs_rank")

    # -- Signal A: sector ETF above 50-day MA and trending up (0.5 pts) --
    sig_sector = bool(etf_data.get("trending_up", False))

    # -- Signal B: bull market regime (0.5 pts) --
    sig_bull = regime.get("regime") == "bull"

    # -- Signal C: top-N RS rank within sector (0.25 pts) --
    sig_rs = bool(rs_rank is not None and rs_rank <= config.RS_RANK_THRESHOLD)

    # -- Signal D: no macro headwind today (0.25 pts) --
    sig_macro = date.today() not in _MACRO_HEADWIND_DATES

    total = round(
        (0.50 if sig_sector else 0.0)
        + (0.50 if sig_bull   else 0.0)
        + (0.25 if sig_rs     else 0.0)
        + (0.25 if sig_macro  else 0.0),
        2,
    )

    logger.debug(
        "%s L5: sector=%s bull=%s rs_rank=%s(top%d=%s) macro=%s => %.2f",
        ticker, sig_sector, sig_bull,
        rs_rank, config.RS_RANK_THRESHOLD, sig_rs,
        sig_macro, total,
    )

    return {
        "score": total,
        "signals": {
            "sector_aligned": sig_sector,
            "bull_regime":    sig_bull,
            "top5_rs":        sig_rs,
            "macro_clear":    sig_macro,
            "rs_rank":        rs_rank,
            "regime":         regime.get("regime"),
            "sector_etf":     etf,
            "etf_above_50ma": etf_data.get("above_50ma") if etf_data else None,
            "etf_slope":      etf_data.get("slope")      if etf_data else None,
        },
    }
