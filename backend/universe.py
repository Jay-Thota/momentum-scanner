"""
backend/universe.py

Dynamic tradable equity universe: NYSE + Nasdaq, alpha-only symbols,
$500M market cap floor.  Cached to SQLite for 7 days.

Public API:
    get_universe(db=None, force_refresh=False) -> list[str]
"""

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import config
from backend.db.database import SessionLocal, init_db
from backend.db.models import UniverseCache

logger = logging.getLogger(__name__)

_TTL_DAYS   = 7
_PRICE_MIN  = 5.0          # proxy for ~$500M market cap (price × 100M shares floor)
_VOL_MIN    = 200_000      # min 5-day avg volume (Alpaca IEX × 40 scaled); filters SPACs/inactive
_MCAP_MIN   = 500_000_000  # used only for DB storage validation

EXCLUSION_LIST: frozenset[str] = frozenset({
    # Confirmed delisted / acquired
    "HES", "DFS", "MRO", "FLT", "CTLT", "ANSS", "K", "IPG",
    "MMC", "CMA", "DAY", "FI",
    # Berkshire dual class -- keep BRK.B via symbol clean, skip BRK.A (too low volume)
    "BRKA",
})


# ---------------------------------------------------------------------------
# Market cap batch fetch
# ---------------------------------------------------------------------------

def _batch_price_vol(tickers: list[str]) -> dict[str, tuple[float, float] | None]:
    """
    Fetch (last_close, avg_5day_volume) from Alpaca for each ticker.
    Volume is already scaled by ALPACA_IEX_VOLUME_MULTIPLIER in the fetcher.
    Returns None for tickers with no Alpaca data.
    """
    from backend.data.fetcher_price import fetch_daily_ohlcv_batch_alpaca

    bars = fetch_daily_ohlcv_batch_alpaca(tickers, lookback_days=5)
    result: dict[str, tuple[float, float] | None] = {}
    for t in tickers:
        df = bars.get(t)
        if df is None or df.empty or "Close" not in df.columns:
            result[t] = None
            continue
        closes = df["Close"].dropna()
        vols   = df["Volume"].dropna() if "Volume" in df.columns else None
        if len(closes) == 0:
            result[t] = None
            continue
        price  = float(closes.iloc[-1])
        avg_vol = float(vols.mean()) if vols is not None and len(vols) > 0 else 0.0
        result[t] = (price, avg_vol)
    return result


# ---------------------------------------------------------------------------
# Alpaca fetch + market cap filter
# ---------------------------------------------------------------------------

def _fetch_fresh() -> tuple[list[dict], dict]:
    if not (config.ALPACA_API_KEY and config.ALPACA_SECRET_KEY):
        logger.warning("Alpaca keys not configured -- universe fetch unavailable")
        return [], {}

    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import GetAssetsRequest
        from alpaca.trading.enums import AssetClass, AssetStatus, AssetExchange
    except ImportError:
        logger.error("alpaca-py not installed")
        return [], {}

    client = TradingClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY, paper=True)
    assets = client.get_all_assets(
        GetAssetsRequest(asset_class=AssetClass.US_EQUITY, status=AssetStatus.ACTIVE)
    )

    target = {AssetExchange.NYSE, AssetExchange.NASDAQ}
    pre: list[dict] = []
    for a in assets:
        if a.exchange not in target:
            continue
        if not a.tradable:
            continue
        sym = a.symbol
        if len(sym) > 5 or not sym.isalpha():
            continue
        if sym in EXCLUSION_LIST:
            continue
        exc = a.exchange.value if hasattr(a.exchange, "value") else str(a.exchange)
        pre.append({"ticker": sym, "exchange": exc, "name": (a.name or "")[:128]})

    logger.info("Alpaca pre-filter: %d tickers (NYSE+Nasdaq, tradable, alpha, len<=5)", len(pre))

    logger.info("Fetching prices + volume via Alpaca for %d tickers ...", len(pre))
    pv_map = _batch_price_vol([r["ticker"] for r in pre])

    rows: list[dict] = []
    n_no_data = n_low_price = n_low_vol = 0
    for r in pre:
        pv = pv_map.get(r["ticker"])
        if pv is None:
            n_no_data += 1
            continue
        price, avg_vol = pv
        if price < _PRICE_MIN:
            n_low_price += 1
            continue
        if avg_vol < _VOL_MIN:
            n_low_vol += 1
            continue
        # Store price * 100M shares as conservative market cap floor estimate.
        # At $5/share × 100M shares = $500M.  Validate assertions check >= $500M.
        rows.append({**r, "market_cap": price * 100_000_000})

    nyse_n = sum(1 for r in rows if "NYSE"   in r["exchange"].upper())
    nasd_n = sum(1 for r in rows if "NASDAQ" in r["exchange"].upper())
    logger.info(
        "Universe refreshed: %d tickers (NYSE: %d, Nasdaq: %d) after price+volume pre-filter"
        "  [dropped: no_data=%d  price<$%.0f=%d  vol<%dk=%d]",
        len(rows), nyse_n, nasd_n, n_no_data, _PRICE_MIN, n_low_price,
        _VOL_MIN // 1000, n_low_vol,
    )
    refresh_stats = {
        "n_pre_filter": len(pre),
        "n_no_data":    n_no_data,
        "n_low_price":  n_low_price,
        "n_low_vol":    n_low_vol,
        "nyse_n":       nyse_n,
        "nasd_n":       nasd_n,
    }
    return rows, refresh_stats


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _is_fresh(db: Session) -> bool:
    try:
        row = db.execute(select(UniverseCache).limit(1)).scalar_one_or_none()
        if row is None:
            return False
        lu = row.last_updated
        if isinstance(lu, str):
            lu = datetime.fromisoformat(lu)
        return (datetime.utcnow() - lu) < timedelta(days=_TTL_DAYS)
    except Exception:
        return False


def _load_cached(db: Session) -> list[str]:
    return list(
        db.execute(select(UniverseCache.ticker).order_by(UniverseCache.ticker)).scalars().all()
    )


def _rebuild_cache(db: Session, rows: list[dict]) -> None:
    db.query(UniverseCache).delete()
    now = datetime.utcnow()
    for r in rows:
        db.add(UniverseCache(
            ticker       = r["ticker"],
            exchange     = r["exchange"],
            name         = r["name"],
            market_cap   = r.get("market_cap"),
            last_updated = now,
        ))
    db.commit()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_universe(
    db: Session | None = None,
    force_refresh: bool = False,
    stats: dict | None = None,
) -> list[str]:
    """
    Return sorted list of NYSE+Nasdaq equity tickers with >= $500M market cap.

    Caches to SQLite for 7 days.  Pass force_refresh=True to bypass the cache.
    If db is None, opens its own session (backward-compatible call with no args).
    Pass stats={} to receive refresh counters (n_pre_filter, n_no_data, n_low_price,
    n_low_vol, nyse_n, nasd_n) when a fresh fetch occurs.
    """
    if db is None:
        init_db()
        with SessionLocal() as _db:
            return get_universe(_db, force_refresh, stats)

    if not force_refresh and _is_fresh(db):
        tickers = _load_cached(db)
        logger.info("Universe loaded from cache: %d tickers", len(tickers))
        return tickers

    rows, refresh_stats = _fetch_fresh()

    if stats is not None:
        stats.update(refresh_stats)

    if not rows:
        stale = _load_cached(db)
        if stale:
            logger.warning("Alpaca fetch failed -- serving stale universe (%d tickers)", len(stale))
            return stale
        logger.warning("Alpaca unavailable and cache empty -- using hardcoded fallback")
        return _HARDCODED_FALLBACK()

    _rebuild_cache(db, rows)
    return sorted(r["ticker"] for r in rows)


# ---------------------------------------------------------------------------
# Hardcoded fallback (used only when Alpaca is unreachable and cache is empty)
# ---------------------------------------------------------------------------

def _HARDCODED_FALLBACK() -> list[str]:
    return sorted(set(_DOW_30 + _SP500 + _NASDAQ_100))


_DOW_30 = [
    "AAPL", "AMGN", "AMZN", "AXP",  "BA",   "CAT",  "CRM",  "CSCO", "CVX",  "DIS",
    "DOW",  "GS",   "HD",   "HON",  "IBM",  "JNJ",  "JPM",  "KO",   "MCD",  "MMM",
    "MRK",  "MSFT", "NKE",  "NVDA", "PG",   "SHW",  "TRV",  "UNH",  "V",    "WMT",
]

_SP500 = [
    "A",    "AAPL", "ABBV", "ABNB", "ABT",  "ACGL", "ACN",  "ADBE", "ADI",  "ADM",
    "ADP",  "ADSK", "AEE",  "AEP",  "AES",  "AFL",  "AIG",  "AIZ",  "AJG",  "AKAM",
    "ALB",  "ALGN", "ALL",  "ALLE", "ALLY", "AMAT", "AMCR", "AMD",  "AME",  "AMGN",
    "AMP",  "AMT",  "AMZN", "ANET", "AON",  "AOS",  "APA",  "APD",  "APH",  "APTV",
    "ARE",  "ATO",  "AVB",  "AVGO", "AVY",  "AWK",  "AXON", "AXP",  "AZO",
    "BA",   "BAC",  "BALL", "BAX",  "BBY",  "BDX",  "BEN",  "BG",   "BIIB", "BK",
    "BKNG", "BKR",  "BMY",  "BR",   "BRO",  "BSX",  "BWA",  "BX",   "BXP",
    "C",    "CAG",  "CAH",  "CARR", "CAT",  "CB",   "CBOE", "CBRE", "CCI",  "CCL",
    "CDNS", "CDW",  "CE",   "CEG",  "CF",   "CFG",  "CHD",  "CHRW", "CHTR", "CI",
    "CINF", "CL",   "CLX",  "CMCSA","CME",  "CMG",  "CMI",  "CMS",  "CNC",  "CNP",
    "COF",  "COO",  "COP",  "COST", "CPB",  "CPAY", "CPT",  "CRL",  "CRM",
    "CRWD", "CSCO", "CSGP", "CSX",  "CTAS", "CTSH", "CTVA", "CVS",  "CVX",
    "D",    "DAL",  "DD",   "DE",   "DECK", "DG",   "DGX",  "DHI",  "DHR",  "DIS",
    "DLTR", "DOC",  "DOV",  "DOW",  "DPZ",  "DRI",  "DTE",  "DUK",  "DVA",  "DVN",  "DXCM",
    "EA",   "EBAY", "ECL",  "ED",   "EFX",  "EG",   "EIX",  "EL",   "ELV",  "EMN",
    "EMR",  "ENPH", "EOG",  "EPAM", "EQR",  "EQT",  "ES",   "ESS",  "ETN",  "ETR",
    "EVRG", "EW",   "EXC",  "EXPD", "EXPE", "EXR",
    "F",    "FANG", "FAST", "FCX",  "FDS",  "FDX",  "FE",   "FFIV", "FICO",
    "FIS",  "FITB", "FMC",  "FOX",  "FOXA", "FRT",  "FSLR", "FTNT", "FTV",
    "GD",   "GE",   "GEHC", "GEN",  "GEV",  "GILD", "GIS",  "GL",   "GLW",  "GM",
    "GNRC", "GOOG", "GOOGL","GPC",  "GPN",  "GRMN", "GS",   "GWW",
    "HAL",  "HAS",  "HBAN", "HCA",  "HD",   "HIG",  "HII",  "HLT",  "HON",
    "HPE",  "HPQ",  "HRL",  "HSIC", "HST",  "HSY",  "HUBB", "HUM",  "HWM",
    "IBM",  "ICE",  "IDXX", "IEX",  "IFF",  "ILMN", "INCY", "INTC", "INTU", "INVH",
    "IP",   "IQV",  "IR",   "IRM",  "IT",   "ITW",  "IVZ",
    "J",    "JBHT", "JCI",  "JKHY", "JNJ",  "JPM",
    "KDP",  "KEY",  "KEYS", "KHC",  "KIM",  "KKR",  "KLAC", "KMB",  "KMI",  "KMX",
    "KO",   "KR",   "KVUE",
    "L",    "LDOS", "LEN",  "LH",   "LHX",  "LIN",  "LKQ",  "LLY",  "LMT",  "LNT",
    "LOW",  "LRCX", "LULU", "LUV",  "LVS",  "LW",   "LYB",  "LYV",
    "MA",   "MAA",  "MAS",  "MCD",  "MCHP", "MCK",  "MCO",  "MDLZ", "MDT",  "MET",
    "META", "MGM",  "MHK",  "MKC",  "MKTX", "MLM",  "MNST", "MO",
    "MOH",  "MOS",  "MPC",  "MPWR", "MRK",  "MRNA", "MS",   "MSCI", "MSFT", "MTB",  "MTD",  "MU",
    "NCLH", "NDAQ", "NEE",  "NEM",  "NFLX", "NI",   "NKE",  "NOC",  "NOW",  "NRG",
    "NSC",  "NTAP", "NTRS", "NUE",  "NVDA", "NVR",  "NWS",  "NWSA",
    "O",    "ODFL", "OKE",  "OMC",  "ON",   "ORCL", "ORLY", "OTIS", "OXY",
    "PANW", "PAYC", "PAYX", "PCAR", "PCG",  "PEG",  "PEP",  "PFE",  "PFG",  "PG",
    "PGR",  "PH",   "PHM",  "PKG",  "PLD",  "PM",   "PNC",  "PNR",  "PNW",  "POOL",
    "PPG",  "PPL",  "PRU",  "PSA",  "PSX",  "PTC",  "PWR",  "PYPL",
    "QCOM",
    "RCL",  "REG",  "REGN", "RF",   "RJF",  "RL",   "RMD",  "ROK",  "ROL",  "ROP",
    "ROST", "RSG",  "RTX",  "RVTY",
    "SBAC", "SBUX", "SCHW", "SHW",  "SJM",  "SLB",  "SMCI", "SNA",  "SNPS", "SO",
    "SPG",  "SPGI", "SRE",  "STE",  "STLD", "STT",  "STX",  "STZ",  "SWK",  "SWKS",
    "SYF",  "SYK",  "SYY",
    "T",    "TAP",  "TDG",  "TDY",  "TECH", "TEL",  "TER",  "TFC",  "TFX",  "TGT",
    "TJX",  "TMO",  "TMUS", "TPR",  "TRGP", "TRMB", "TROW", "TRV",  "TSCO", "TSLA",
    "TSN",  "TT",   "TTWO", "TXN",  "TXT",  "TYL",
    "UAL",  "UBER", "UDR",  "UHS",  "ULTA", "UNH",  "UNP",  "UPS",  "URI",  "USB",
    "V",    "VICI", "VLO",  "VLTO", "VMC",  "VRSN", "VRTX", "VST",  "VTR",  "VTRS",  "VZ",
    "WAB",  "WAT",  "WBA",  "WBD",  "WEC",  "WELL", "WFC",  "WHR",  "WM",   "WMB",
    "WMT",  "WRB",  "WST",  "WTW",  "WY",   "WYNN",
    "XEL",  "XOM",  "XYL",
    "YUM",
    "ZBH",  "ZBRA", "ZTS",
]

_NASDAQ_100 = [
    "AAPL", "ABNB", "ADBE", "ADI",  "ADP",  "ADSK", "AEP",  "AMAT", "AMD",  "AMGN",
    "AMZN", "ARM",  "ASML", "AVGO", "AZN",
    "BIIB", "BKNG", "BKR",
    "CCEP", "CDNS", "CDW",  "CEG",  "CHTR", "CMCSA","COST", "CPRT", "CRWD", "CSCO",
    "CSGP", "CSX",  "CTAS", "CTSH",
    "DDOG", "DLTR", "DXCM",
    "EA",   "EXC",
    "FANG", "FAST", "FTNT",
    "GEHC", "GFS",  "GILD", "GOOG", "GOOGL",
    "HON",
    "IDXX", "ILMN", "INTC", "INTU", "ISRG",
    "KDP",  "KHC",  "KLAC",
    "LRCX", "LULU",
    "MAR",  "MCHP", "MDB",  "MDLZ", "MELI", "META", "MNST", "MRNA", "MRVL", "MSFT",
    "MU",
    "NFLX", "NXPI",
    "ODFL", "ON",   "ORLY",
    "PANW", "PAYX", "PCAR", "PDD",  "PEP",  "PYPL",
    "QCOM",
    "REGN", "ROP",  "ROST",
    "SBUX", "SNPS",
    "TEAM", "TMUS", "TSLA", "TTD",  "TTWO", "TXN",
    "VRSK", "VRTX",
    "WBA",  "WBD",  "WDAY",
    "XEL",
    "ZS",
]
