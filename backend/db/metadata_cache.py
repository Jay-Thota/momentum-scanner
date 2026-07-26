"""
backend/db/metadata_cache.py

The ONLY place in the codebase that calls yf.Ticker(ticker).info.

Fetches and caches market_cap, sector, etf_symbol, earnings_next_date,
si_pct, and days_to_cover with a 7-day TTL.  On subsequent scan runs the
hot path never touches Yahoo for any of these fields.

Public API:
    get_metadata(ticker, session)                -> dict
    get_metadata_batch(tickers, session, stats)  -> dict[str, dict]
"""

import logging
import time
from datetime import date, datetime

import yfinance as yf
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.data.fetcher_sector import SECTOR_ETF_MAP
from backend.db.models import MetadataCache

logger = logging.getLogger(__name__)

TTL_DAYS = 7
_FETCH_DELAY_S = 0.5   # seconds between Yahoo calls in batch mode

_YAHOO_ALIAS: dict[str, str] = {"BRK.B": "BRK-B", "BF.B": "BF-B"}

# Bond/fund ETFs that yfinance returns 404 for -- skip without an HTTP call.
_SKIP_TICKERS: frozenset[str] = frozenset({"BOND", "BSCU", "EMB", "IGIB"})

_EMPTY: dict = {
    "market_cap":         0.0,
    "sector":             None,
    "etf_symbol":         None,
    "earnings_next_date": None,
    "si_pct":             None,
    "days_to_cover":      None,
    "eps_growth":         None,   # True/False/None (stored as 1.0/0.0/None in DB)
    "revenue_growth":     None,   # fractional YoY (e.g. 0.12 = 12%)
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_fresh(last_updated: str | None) -> bool:
    if not last_updated:
        return False
    try:
        return (datetime.now() - datetime.fromisoformat(last_updated)).days < TTL_DAYS
    except Exception:
        return False


def _row_to_dict(row: MetadataCache) -> dict:
    eg = row.eps_growth
    return {
        "market_cap":         row.market_cap or 0.0,
        "sector":             row.sector,
        "etf_symbol":         row.etf_symbol,
        "earnings_next_date": row.earnings_next_date,
        "si_pct":             row.si_pct,
        "days_to_cover":      row.days_to_cover,
        "eps_growth":         bool(eg) if eg is not None else None,
        "revenue_growth":     row.revenue_growth,
    }


def _fetch_from_yahoo(ticker: str) -> dict:
    """
    Single Yahoo Finance fetch for all cacheable metadata.
    Called only when the cache is stale or missing.
    Tickers in _SKIP_TICKERS return empty metadata immediately without an HTTP call.
    """
    if ticker in _SKIP_TICKERS:
        logger.debug("_fetch_from_yahoo %s: skipped (known bond/fund, no yfinance data)", ticker)
        return dict(_EMPTY)

    yahoo_sym = _YAHOO_ALIAS.get(ticker, ticker)
    out = dict(_EMPTY)
    try:
        t    = yf.Ticker(yahoo_sym)
        info = t.info

        mc = info.get("marketCap")
        out["market_cap"] = float(mc) if mc is not None else 0.0

        sector = info.get("sector")
        out["sector"]    = sector
        out["etf_symbol"] = SECTOR_ETF_MAP.get(sector) if sector else None

        si  = info.get("shortPercentOfFloat")
        dtc = info.get("shortRatio")
        out["si_pct"]        = float(si)  if si  is not None else None
        out["days_to_cover"] = float(dtc) if dtc is not None else None

        try:
            cal  = t.calendar
            earn = cal.get("Earnings Date", []) if isinstance(cal, dict) else []
            if earn:
                d = earn[0]
                if isinstance(d, str):
                    out["earnings_next_date"] = d[:10]
                elif isinstance(d, date):
                    out["earnings_next_date"] = d.isoformat()
                else:
                    out["earnings_next_date"] = d.date().isoformat()
        except Exception:
            pass

        # -- Fundamentals from .financials (same cache TTL, no extra warm-run call) --
        try:
            import pandas as _pd
            fin = t.financials   # rows=items, cols=dates newest-first
            if not fin.empty and fin.shape[1] >= 2:
                if "Net Income" in fin.index:
                    ni_cur = fin.loc["Net Income"].iloc[0]
                    ni_prv = fin.loc["Net Income"].iloc[1]
                    if _pd.notna(ni_cur) and _pd.notna(ni_prv):
                        out["eps_growth"] = bool(ni_cur > 0 and ni_cur > ni_prv)
                for rev_key in ("Total Revenue", "Operating Revenue"):
                    if rev_key in fin.index:
                        r_cur = fin.loc[rev_key].iloc[0]
                        r_prv = fin.loc[rev_key].iloc[1]
                        if _pd.notna(r_cur) and _pd.notna(r_prv) and r_prv > 0:
                            out["revenue_growth"] = float((r_cur - r_prv) / r_prv)
                        break
        except Exception:
            pass   # fundamentals unavailable -- eps_growth/revenue_growth stay None

    except Exception as exc:
        _msg = str(exc)
        if "404" in _msg or "No data found" in _msg or "JSONDecodeError" in type(exc).__name__:
            logger.debug("_fetch_from_yahoo %s: not found (%s)", ticker, _msg)
        else:
            logger.warning("_fetch_from_yahoo %s: %s", ticker, exc)
    return out


def _upsert(session: Session, ticker: str, data: dict, row: MetadataCache | None) -> None:
    if row is None:
        row = MetadataCache(ticker=ticker)
        session.add(row)
    row.market_cap         = data["market_cap"]
    row.sector             = data["sector"]
    row.etf_symbol         = data["etf_symbol"]
    row.earnings_next_date = data["earnings_next_date"]
    row.si_pct             = data["si_pct"]
    row.days_to_cover      = data["days_to_cover"]
    # Store bool as float so SQLite Column(Float) accepts it cleanly.
    eg = data.get("eps_growth")
    row.eps_growth         = (1.0 if eg else 0.0) if eg is not None else None
    row.revenue_growth     = data.get("revenue_growth")
    row.last_updated       = datetime.now().isoformat()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_metadata(ticker: str, session: Session) -> dict:
    """Return cached metadata if fresh, otherwise fetch from Yahoo and cache."""
    row = session.execute(
        select(MetadataCache).where(MetadataCache.ticker == ticker)
    ).scalar_one_or_none()

    if row and _is_fresh(row.last_updated):
        return _row_to_dict(row)

    data = _fetch_from_yahoo(ticker)
    _upsert(session, ticker, data, row)
    session.commit()
    return data


def get_metadata_batch(
    tickers: list[str],
    session: Session,
    stats: dict | None = None,
) -> dict[str, dict]:
    """
    Batch metadata fetch with cache-first strategy.

    Fresh cached entries are returned immediately.  Stale/missing tickers are
    fetched from Yahoo one at a time with a 0.5s inter-call delay to avoid
    rate-limiting.

    stats (optional) is populated with:
        metadata_cache_hits    -- number of tickers served from cache
        metadata_yahoo_calls   -- number of Yahoo .info calls made
    """
    if not tickers:
        return {}

    result:   dict[str, dict] = {}
    to_fetch: list[tuple[str, MetadataCache | None]] = []

    # Load all existing cache rows in a single query
    cached: dict[str, MetadataCache] = {
        row.ticker: row
        for row in session.execute(
            select(MetadataCache).where(MetadataCache.ticker.in_(tickers))
        ).scalars().all()
    }

    for ticker in tickers:
        row = cached.get(ticker)
        if row and _is_fresh(row.last_updated):
            result[ticker] = _row_to_dict(row)
        else:
            to_fetch.append((ticker, row))

    if stats is not None:
        stats["metadata_cache_hits"]  = len(result)
        stats["metadata_yahoo_calls"] = len(to_fetch)

    logger.info(
        "MetadataCache: %d cache hits, %d Yahoo calls needed",
        len(result), len(to_fetch),
    )

    for i, (ticker, existing_row) in enumerate(to_fetch):
        if i > 0:
            time.sleep(_FETCH_DELAY_S)
        data = _fetch_from_yahoo(ticker)
        _upsert(session, ticker, data, existing_row)
        result[ticker] = data

    if to_fetch:
        session.commit()

    return result
