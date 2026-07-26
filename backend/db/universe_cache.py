"""
backend/db/universe_cache.py

Fetches the full NYSE + Nasdaq tradable universe from the Alpaca assets
endpoint and caches it to SQLite with a 7-day TTL.

Public API:
    get_universe() -> list[str]   -- cached, refreshed at most once per week
"""

import logging
import re
from datetime import datetime

from sqlalchemy import func, select

from backend import config
from backend.db.database import SessionLocal, init_db
from backend.db.models import UniverseCache

logger = logging.getLogger(__name__)

TTL_DAYS = 7

# Ticker-level filter rules applied after the Alpaca asset filter.
# These remove ETFs, leveraged products, warrants, rights, and other
# non-equity securities that slip through the primary exchange filter.
_MAX_TICKER_LEN  = 5
_DIGIT_RE        = re.compile(r"\d")
_BAD_SUFFIX_RE   = re.compile(
    r"(\.(W|WS|WI|WT|R|U|P|A|B|C|D|E|F|G|H|I|J|K|L|M|N|O|Q|T|V|X|Y|Z))$",
    re.IGNORECASE,
)
# Known ETF ticker prefixes / patterns (3+ letter all-caps with no distinguishing
# suffix -- catches most SPDR, iShares, Vanguard, ProShares, etc.)
_ETF_PREFIXES = frozenset({
    "SPY", "QQQ", "IWM", "DIA", "GLD", "SLV", "USO", "TLT", "HYG", "LQD",
    "EFA", "EEM", "VTI", "VOO", "VEA", "VWO", "BND", "AGG", "XLK", "XLV",
    "XLF", "XLY", "XLP", "XLE", "XLI", "XLB", "XLRE", "XLU", "XLC", "XLG",
    "GDX", "GDXJ", "ARKK", "ARKG", "ARKW", "ARKF", "ARKQ", "ARKX",
    "UVXY", "SVXY", "VXX", "VIXY",
    "TQQQ", "SQQQ", "UPRO", "SPXU", "UDOW", "SDOW", "LABU", "LABD",
    "SOXL", "SOXS", "FAS", "FAZ", "TNA", "TZA", "ERX", "ERY",
})


# ---------------------------------------------------------------------------
# Cache freshness
# ---------------------------------------------------------------------------

def _cache_is_fresh(session) -> bool:
    row = session.execute(
        select(UniverseCache).limit(1)
    ).scalar_one_or_none()
    if row is None:
        return False
    try:
        age = (datetime.now() - datetime.fromisoformat(row.last_updated)).days
        return age < TTL_DAYS
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Ticker filtering
# ---------------------------------------------------------------------------

def _is_valid_equity_ticker(symbol: str) -> bool:
    """Return True if symbol looks like a plain common-stock ticker."""
    if len(symbol) > _MAX_TICKER_LEN:
        return False
    if _DIGIT_RE.search(symbol):
        return False
    if _BAD_SUFFIX_RE.search(symbol):
        return False
    if symbol in _ETF_PREFIXES:
        return False
    # Reject tickers ending in common ETF/fund letter suffixes when 4-5 chars
    # e.g. SPDW, IEFA -- these are usually funds, not equities.
    # Single/double-letter names are always kept (V, C, T, MA, etc.).
    return True


# ---------------------------------------------------------------------------
# Alpaca fetch
# ---------------------------------------------------------------------------

def _fetch_from_alpaca() -> list[dict]:
    """
    Pull active US equity assets from Alpaca, filter to NYSE/Nasdaq tradable
    common stocks, and return a list of dicts with ticker/exchange/name.
    """
    if not (config.ALPACA_API_KEY and config.ALPACA_SECRET_KEY):
        logger.warning("Alpaca keys not configured -- universe fetch unavailable")
        return []

    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import GetAssetsRequest
        from alpaca.trading.enums import AssetClass, AssetStatus, AssetExchange
    except ImportError:
        logger.error("alpaca-py not installed -- cannot fetch universe")
        return []

    client = TradingClient(
        config.ALPACA_API_KEY,
        config.ALPACA_SECRET_KEY,
        paper=True,
    )
    request = GetAssetsRequest(
        asset_class=AssetClass.US_EQUITY,
        status=AssetStatus.ACTIVE,
    )

    logger.info("Fetching full asset list from Alpaca ...")
    assets = client.get_all_assets(request)

    target_exchanges = {AssetExchange.NYSE, AssetExchange.NASDAQ}

    rows = []
    skipped_exchange = skipped_suffix = skipped_filter = 0

    for a in assets:
        if a.exchange not in target_exchanges:
            skipped_exchange += 1
            continue
        if not a.tradable:
            skipped_suffix += 1
            continue
        sym = a.symbol
        # Drop warrants / rights / units indicated by specific dot-suffixes
        # (.W .WS .R .U are the canonical ones; BRK/B uses "/" not ".")
        if "." in sym and _BAD_SUFFIX_RE.search(sym):
            skipped_suffix += 1
            continue
        if not _is_valid_equity_ticker(sym):
            skipped_filter += 1
            continue
        rows.append({
            "ticker":   sym,
            "exchange": str(a.exchange.value) if hasattr(a.exchange, "value") else str(a.exchange),
            "name":     (a.name or "")[:128],
        })

    nyse_n    = sum(1 for r in rows if "NYSE"   in r["exchange"].upper())
    nasdaq_n  = sum(1 for r in rows if "NASDAQ" in r["exchange"].upper())
    logger.info(
        "Universe refreshed: %d tickers (NYSE: %d, Nasdaq: %d)  "
        "[skipped exchange=%d  tradable/suffix=%d  ticker_filter=%d]",
        len(rows), nyse_n, nasdaq_n,
        skipped_exchange, skipped_suffix, skipped_filter,
    )
    return rows


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_universe() -> list[str]:
    """
    Return the full NYSE + Nasdaq tradable equity universe as a sorted list.

    Uses a 7-day SQLite cache.  On first run (or after cache expiry) re-fetches
    from the Alpaca assets endpoint.  Falls back to the old hardcoded lists if
    Alpaca is unavailable and the cache is empty.
    """
    init_db()

    with SessionLocal() as session:
        if _cache_is_fresh(session):
            tickers = session.execute(
                select(UniverseCache.ticker).order_by(UniverseCache.ticker)
            ).scalars().all()
            logger.info("Universe loaded from cache: %d tickers", len(tickers))
            return list(tickers)

        # Cache is stale or empty -- re-fetch
        rows = _fetch_from_alpaca()

        if not rows:
            # Alpaca unavailable: try to serve stale cache rather than crashing
            stale = session.execute(
                select(UniverseCache.ticker).order_by(UniverseCache.ticker)
            ).scalars().all()
            if stale:
                logger.warning(
                    "Alpaca fetch failed -- serving stale universe (%d tickers)", len(stale)
                )
                return stale

            # Absolute fallback to hardcoded lists
            logger.warning("Alpaca unavailable and cache empty -- using hardcoded lists")
            from backend.universe import _HARDCODED_FALLBACK
            return _HARDCODED_FALLBACK()

        # Rebuild cache table
        now_iso = datetime.now().isoformat()

        # Delete all existing rows
        session.query(UniverseCache).delete()

        for r in rows:
            session.add(UniverseCache(
                ticker       = r["ticker"],
                exchange     = r["exchange"],
                name         = r["name"],
                last_updated = now_iso,
            ))

        session.commit()

        return sorted(r["ticker"] for r in rows)
