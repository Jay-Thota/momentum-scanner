"""
Layer 4: Short Interest Gate.

Hard disqualifies a ticker (returns None) when ANY of:
    si_pct        >= SHORT_INTEREST_MAX_PCT / 100   (e.g. >= 0.10 = 10%)
    days_to_cover >= DAYS_TO_COVER_MAX              (e.g. >= 3.0 days)
    si_trend      == "rising"

When the gate passes, a bonus score 0.0-1.0 is returned:
    +0.25  eps_growth_positive  -- most recent annual Net Income positive AND growing
    +0.25  revenue_growth_5pct  -- most recent annual revenue growth >= REVENUE_GROWTH_MIN
    +0.25  no_activist_short    -- default True (paid data required for override)
    +0.25  borrow_rate_low      -- default True (paid securities lending feed required)
"""

import logging

import pandas as pd
import yfinance as yf

from backend import config

logger = logging.getLogger(__name__)

YAHOO_ALIAS: dict[str, str] = {"BRK.B": "BRK-B", "BF.B": "BF-B"}


# ---------------------------------------------------------------------------
# Internal: fundamentals
# ---------------------------------------------------------------------------

def _fetch_fundamentals(ticker: str) -> dict:
    """
    Return most recent annual EPS and revenue growth from yfinance .financials.

    Keys:
        eps_growth     bool | None  -- Net Income positive AND year-over-year increase
        revenue_growth float | None -- (rev_cur - rev_prv) / rev_prv
    """
    yahoo_sym = YAHOO_ALIAS.get(ticker, ticker)
    out: dict = {"eps_growth": None, "revenue_growth": None}
    try:
        fin = yf.Ticker(yahoo_sym).financials  # rows=items, cols=dates (newest first)

        if fin.empty or fin.shape[1] < 2:
            logger.debug("%s: financials unavailable or only one period", ticker)
            return out

        # -- EPS proxy: Net Income positive and growing --
        if "Net Income" in fin.index:
            ni_cur = fin.loc["Net Income"].iloc[0]
            ni_prv = fin.loc["Net Income"].iloc[1]
            if pd.notna(ni_cur) and pd.notna(ni_prv):
                out["eps_growth"] = bool(ni_cur > 0 and ni_cur > ni_prv)

        # -- Revenue growth --
        for rev_key in ("Total Revenue", "Operating Revenue"):
            if rev_key in fin.index:
                r_cur = fin.loc[rev_key].iloc[0]
                r_prv = fin.loc[rev_key].iloc[1]
                if pd.notna(r_cur) and pd.notna(r_prv) and r_prv > 0:
                    out["revenue_growth"] = float((r_cur - r_prv) / r_prv)
                break

    except Exception as exc:
        logger.debug("_fetch_fundamentals %s: %s", ticker, exc)
    return out


# ---------------------------------------------------------------------------
# Public: score
# ---------------------------------------------------------------------------

def score(data_bundle: dict) -> float | None:
    """
    Layer 4 scorer.

    data_bundle keys:
        ticker         str
        si_pct         float | None  -- decimal fraction (0.10 = 10%)
        days_to_cover  float | None
        si_trend       str           -- "rising" | "flat" | "declining"
        eps_growth     bool | None   -- from MetadataCache (no Yahoo call)
        revenue_growth float | None  -- from MetadataCache (no Yahoo call)

    Returns None (hard disqualify) or a bonus float in [0.0, 1.0].
    Makes zero Yahoo calls -- all inputs come from MetadataCache.
    """
    ticker        = data_bundle.get("ticker", "?")
    si_pct        = data_bundle.get("si_pct")
    days_to_cover = data_bundle.get("days_to_cover")
    si_trend      = data_bundle.get("si_trend", "flat")

    # -- Hard gates (check in order; log the first that fires) --
    if si_pct is not None and si_pct >= config.SHORT_INTEREST_MAX_PCT / 100.0:
        logger.info(
            "Layer 4 DISQUALIFY %s: si_pct=%.1f%% >= threshold %.0f%%",
            ticker, si_pct * 100, config.SHORT_INTEREST_MAX_PCT,
        )
        return None

    if days_to_cover is not None and days_to_cover >= config.DAYS_TO_COVER_MAX:
        logger.info(
            "Layer 4 DISQUALIFY %s: days_to_cover=%.2f >= threshold %.1f",
            ticker, days_to_cover, config.DAYS_TO_COVER_MAX,
        )
        return None

    if si_trend == "rising":
        logger.info("Layer 4 DISQUALIFY %s: si_trend=rising", ticker)
        return None

    # -- Bonus signals (all from MetadataCache, no Yahoo call) --
    sig_eps    = bool(data_bundle.get("eps_growth"))
    rev_growth = data_bundle.get("revenue_growth")
    sig_rev    = bool(rev_growth is not None and rev_growth >= config.REVENUE_GROWTH_MIN)

    # These require paid data feeds; default True and log caveat once per ticker
    sig_activist = True
    sig_borrow   = True
    logger.info(
        "%s no_activist_short=True (default -- activist-short data requires paid feed)",
        ticker,
    )
    logger.info(
        "%s borrow_rate_low=True (default -- securities lending data requires paid feed)",
        ticker,
    )

    bonus = round(
        0.25 * int(sig_eps)
        + 0.25 * int(sig_rev)
        + 0.25 * int(sig_activist)
        + 0.25 * int(sig_borrow),
        2,
    )

    logger.debug(
        "%s Layer 4 PASS: si=%.1f%% dtc=%.2f trend=%s | eps=%s rev=%s activist=%s borrow=%s => bonus=%.2f",
        ticker,
        (si_pct or 0.0) * 100,
        days_to_cover or 0.0,
        si_trend,
        sig_eps, sig_rev, sig_activist, sig_borrow,
        bonus,
    )

    return bonus
