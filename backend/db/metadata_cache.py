"""
backend/db/metadata_cache.py

The ONLY place in the codebase that calls yf.Ticker(ticker).info.

Fetches and caches market_cap, sector, etf_symbol, earnings_next_date,
si_pct, and days_to_cover with a 7-day TTL.  On subsequent scan runs the
hot path never touches Yahoo for any of these fields.

Failed lookups (timeout, 404, ETF/fund) are cached with a 1-day TTL so
the same ticker is not retried within the same trading session.

Public API:
    get_metadata(ticker, session)                -> dict
    get_metadata_batch(tickers, session, stats)  -> dict[str, dict]
"""

import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FuturesTimeout, as_completed
from datetime import date, datetime

import yfinance as yf
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.data.fetcher_sector import SECTOR_ETF_MAP
from backend.db.models import MetadataCache

logger = logging.getLogger(__name__)

TTL_DAYS       = 7    # good data TTL
TTL_DAYS_FAIL  = 1    # ETF / timeout / 404 TTL -- retry tomorrow
_YAHOO_WORKERS = 5    # concurrent Yahoo calls in batch mode
_YAHOO_TIMEOUT = 5    # per-ticker timeout (seconds)

_YAHOO_ALIAS: dict[str, str] = {"BRK.B": "BRK-B", "BF.B": "BF-B"}

# Known 404s -- skip without any HTTP call.
_SKIP_TICKERS: frozenset[str] = frozenset({"BOND", "BSCU", "EMB", "IGIB"})

# market_cap=None signals a failed/ETF/timeout fetch (1-day TTL, skipped by Layer 0).
# market_cap=0.0 means the ticker was fetched but Yahoo returned no marketCap field.
_EMPTY: dict = {
    "market_cap":         None,
    "sector":             None,
    "etf_symbol":         None,
    "earnings_next_date": None,
    "si_pct":             None,
    "days_to_cover":      None,
    "eps_growth":         None,
    "revenue_growth":     None,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_fresh(row: MetadataCache) -> bool:
    if not row.last_updated:
        return False
    try:
        age_days = (datetime.now() - datetime.fromisoformat(row.last_updated)).days
        # Failed/ETF entries (market_cap is None) get a 1-day TTL so they are
        # retried on the next trading day without blocking today's run.
        ttl = TTL_DAYS_FAIL if row.market_cap is None else TTL_DAYS
        return age_days < ttl
    except Exception:
        return False


def _row_to_dict(row: MetadataCache) -> dict:
    eg = row.eps_growth
    return {
        "market_cap":         row.market_cap or 0.0,  # None → 0.0 for L0 gate
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
    Fetch all cacheable metadata for one ticker from Yahoo Finance.

    All network calls run inside a thread with a hard _YAHOO_TIMEOUT-second wall-clock
    deadline.  Returns a dict; market_cap=None means "failed/ETF" and the caller
    should use TTL_DAYS_FAIL when writing to cache.
    """
    if ticker in _SKIP_TICKERS:
        logger.debug("_fetch_from_yahoo %s: skipped (known 404)", ticker)
        return dict(_EMPTY)

    yahoo_sym = _YAHOO_ALIAS.get(ticker, ticker)

    def _all_calls() -> dict:
        out = dict(_EMPTY)
        t    = yf.Ticker(yahoo_sym)
        info = t.info

        # Structurally detect ETFs and mutual funds -- no market cap data useful.
        quote_type = info.get("quoteType", "")
        if quote_type in ("ETF", "MUTUALFUND"):
            logger.debug("_fetch_from_yahoo %s: quoteType=%s -- caching as skip", ticker, quote_type)
            return dict(_EMPTY)  # market_cap stays None → 1-day TTL

        mc = info.get("marketCap")
        out["market_cap"] = float(mc) if mc is not None else 0.0

        sector = info.get("sector")
        out["sector"]     = sector
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

        try:
            import pandas as _pd
            fin = t.financials
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
            pass

        return out

    try:
        with ThreadPoolExecutor(max_workers=1) as _ex:
            _fut = _ex.submit(_all_calls)
            try:
                return _fut.result(timeout=_YAHOO_TIMEOUT)
            except _FuturesTimeout:
                logger.warning(
                    "_fetch_from_yahoo %s: timed out after %ds -- caching as skip",
                    ticker, _YAHOO_TIMEOUT,
                )
                return dict(_EMPTY)  # market_cap=None → 1-day TTL
    except Exception as exc:
        _msg = str(exc)
        if "404" in _msg or "No data found" in _msg or "JSONDecodeError" in type(exc).__name__:
            logger.debug("_fetch_from_yahoo %s: not found (%s)", ticker, _msg)
        else:
            logger.warning("_fetch_from_yahoo %s: %s", ticker, exc)
    return dict(_EMPTY)


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

    if row and _is_fresh(row):
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
    fetched in parallel (5 workers) with a 5-second per-ticker timeout to avoid
    Railway cold-start hangs from Yahoo rate limiting.  All DB writes happen in
    a single serial upsert after the parallel fetch completes.

    stats (optional) is populated with:
        metadata_cache_hits    -- tickers served from cache
        metadata_yahoo_calls   -- Yahoo .info calls attempted
        metadata_skipped       -- ETF/timeout/404 entries cached with 1-day TTL
    """
    if not tickers:
        return {}

    result:   dict[str, dict] = {}
    to_fetch: list[tuple[str, MetadataCache | None]] = []

    # Load all existing cache rows in a single query.
    cached: dict[str, MetadataCache] = {
        row.ticker: row
        for row in session.execute(
            select(MetadataCache).where(MetadataCache.ticker.in_(tickers))
        ).scalars().all()
    }

    for ticker in tickers:
        row = cached.get(ticker)
        if row and _is_fresh(row):
            result[ticker] = _row_to_dict(row)
        else:
            to_fetch.append((ticker, row))

    cache_hits = len(result)
    total      = len(tickers)

    if stats is not None:
        stats["metadata_cache_hits"]  = cache_hits
        stats["metadata_yahoo_calls"] = len(to_fetch)

    logger.info(
        "MetadataCache: %d cache hits, %d Yahoo calls needed",
        cache_hits, len(to_fetch),
    )

    if not to_fetch:
        return result

    # -- Parallel fetch (5 workers, 5s timeout each) --------------------------
    fetched: dict[str, dict] = {}
    skipped = 0
    done    = 0

    with ThreadPoolExecutor(max_workers=_YAHOO_WORKERS) as pool:
        fmap = {pool.submit(_fetch_from_yahoo, ticker): ticker for ticker, _ in to_fetch}
        for fut in as_completed(fmap):
            ticker = fmap[fut]
            done  += 1
            try:
                data = fut.result()
            except Exception as exc:
                logger.debug("metadata fetch error %s: %s", ticker, exc)
                data = dict(_EMPTY)
            fetched[ticker] = data
            if data.get("market_cap") is None:
                skipped += 1
            if done % 50 == 0:
                logger.info(
                    "MetadataCache progress: %d/%d fetched (%d cache hits, %d skipped so far)",
                    cache_hits + done, total, cache_hits, skipped,
                )

    if stats is not None:
        stats["metadata_skipped"] = skipped

    logger.info(
        "MetadataCache fetch complete: %d fetched, %d skipped (ETF/timeout/404)",
        len(fetched), skipped,
    )

    # -- Serial upsert (single session, no write contention) ------------------
    for ticker, existing_row in to_fetch:
        data = fetched.get(ticker, dict(_EMPTY))
        _upsert(session, ticker, data, existing_row)
        result[ticker] = data

    session.commit()
    return result
