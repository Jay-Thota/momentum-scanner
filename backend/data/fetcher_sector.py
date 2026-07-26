"""
backend/data/fetcher_sector.py

Sector ETF momentum signals, stock-to-sector mapping, and relative-strength ranking.

Sector membership in SECTOR_PEERS is a curated subset of the full universe
used for RS rank computation. The full pipeline scorer should pre-build sector
returns once per scan run; validate scripts use build_sector_returns() directly.
"""

import logging
import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import yfinance as yf

from backend import config

logger = logging.getLogger(__name__)

YAHOO_ALIAS: dict[str, str] = {"BRK.B": "BRK-B", "BF.B": "BF-B"}

# Map yfinance .info["sector"] values to SPDR sector ETF symbols
SECTOR_ETF_MAP: dict[str, str] = {
    "Technology":             "XLK",
    "Healthcare":             "XLV",   # yfinance one-word variant
    "Health Care":            "XLV",
    "Financial Services":     "XLF",
    "Financials":             "XLF",
    "Consumer Cyclical":      "XLY",   # yfinance variant
    "Consumer Discretionary": "XLY",
    "Consumer Defensive":     "XLP",   # yfinance variant
    "Consumer Staples":       "XLP",
    "Energy":                 "XLE",
    "Industrials":            "XLI",
    "Basic Materials":        "XLB",
    "Materials":              "XLB",
    "Real Estate":            "XLRE",
    "Utilities":              "XLU",
    "Communication Services": "XLC",
}

# Curated universe peers per sector ETF used for RS rank computation
SECTOR_PEERS: dict[str, list[str]] = {
    "XLK":  ["AAPL","MSFT","NVDA","AVGO","ORCL","ADBE","CSCO","QCOM","TXN","AMD",
             "CRM","INTC","IBM","NOW","INTU","AMAT","KLAC","LRCX","MU","MCHP"],
    "XLV":  ["JNJ","UNH","LLY","ABBV","MRK","TMO","ABT","BMY","DHR","PFE",
             "AMGN","GILD","VRTX","ISRG","REGN","BSX","MDT","EW","BDX","SYK"],
    "XLF":  ["JPM","BAC","WFC","GS","MS","C","BLK","AXP","USB","PNC",
             "TFC","COF","BK","STT","CB","MET","PRU","AFL","TRV"],
    "XLY":  ["AMZN","TSLA","HD","MCD","LOW","NKE","SBUX","TGT","ROST","TJX",
             "GM","F","DG","DLTR","BBY","LEN","DHI","YUM","APTV","BWA"],
    "XLP":  ["PG","KO","PEP","COST","WMT","PM","MO","MDLZ","CL","KHC",
             "GIS","HRL","CPB","SJM","BG"],
    "XLE":  ["XOM","CVX","COP","EOG","SLB","MPC","VLO","PSX","OXY",
             "DVN","EQT","APA","HAL","BKR"],
    "XLI":  ["GE","HON","UNP","RTX","BA","DE","CAT","MMM","LMT","NOC",
             "GD","ITW","EMR","ETN","PH","AME","FTV","SWK"],
    "XLB":  ["LIN","APD","SHW","ECL","FCX","NEM","NUE","STLD","ALB","VMC","MLM","CE"],
    "XLRE": ["AMT","PLD","CCI","EQIX","PSA","EXR","VICI","O","SPG","EQR",
             "AVB","MAA","NNN","WY"],
    "XLU":  ["NEE","DUK","SO","D","AEP","EXC","XEL","ED","ETR","PPL",
             "FE","WEC","ES","CMS","DTE"],
    "XLC":  ["GOOGL","GOOG","META","NFLX","DIS","CMCSA","VZ","T","EA",
             "OMC","TTWO"],
}


# ---------------------------------------------------------------------------
# Sector ETF momentum signals
# ---------------------------------------------------------------------------

def fetch_sector_data(etf: str) -> dict:
    """
    Compute momentum signals for a sector ETF.

    Returns:
        {
            "etf":          str,
            "price":        float | None,
            "ma50":         float | None,
            "above_50ma":   bool,
            "slope":        float | None,   -- fractional slope of MA50 over MA_SLOPE_LOOKBACK
            "trending_up":  bool,           -- above_50ma AND slope > SECTOR_SLOPE_MIN
            "at_52wk_high": bool,           -- price within 2% of 52-week high
        }
    """
    out: dict = {
        "etf":          etf,
        "price":        None,
        "ma50":         None,
        "above_50ma":   False,
        "slope":        None,
        "trending_up":  False,
        "at_52wk_high": False,
    }
    try:
        df = pd.DataFrame()
        for _a in range(3):
            try:
                df = yf.Ticker(etf).history(period="1y", auto_adjust=True)
                break
            except Exception as _e:
                _msg = str(_e)
                if _a < 2 and "Too Many Requests" in _msg:
                    _time.sleep(2.0 ** _a)
                    continue
                if "404" in _msg or "No data found" in _msg or "no timezone" in _msg.lower():
                    logger.debug("fetch_sector_data %s: not found (%s)", etf, _msg)
                    return out
                raise

        if df.empty or len(df) < 51 + config.MA_SLOPE_LOOKBACK:
            logger.debug(
                "fetch_sector_data %s: insufficient history (%d rows)", etf, len(df)
            )
            return out

        close = df["Close"]
        ma50  = close.rolling(50).mean()

        price     = float(close.iloc[-1])
        ma50_val  = float(ma50.iloc[-1])
        lookback  = config.MA_SLOPE_LOOKBACK
        ma50_old  = float(ma50.iloc[-(lookback + 1)])

        slope = (ma50_val - ma50_old) / ma50_old if ma50_old != 0 else 0.0

        above_50ma   = price > ma50_val
        trending_up  = above_50ma and slope > config.SECTOR_SLOPE_MIN
        at_52wk_high = price >= float(close.rolling(252).max().iloc[-1]) * 0.98

        out.update({
            "price":        round(price, 2),
            "ma50":         round(ma50_val, 2),
            "above_50ma":   above_50ma,
            "slope":        round(slope, 6),
            "trending_up":  trending_up,
            "at_52wk_high": at_52wk_high,
        })
    except Exception as exc:
        _msg = str(exc)
        if "404" in _msg or "No data found" in _msg:
            logger.debug("fetch_sector_data %s: not found (%s)", etf, _msg)
        else:
            logger.warning("fetch_sector_data %s: %s", etf, exc)
    return out


# ---------------------------------------------------------------------------
# Stock-to-sector mapping
# ---------------------------------------------------------------------------

def get_stock_sector(ticker: str) -> str | None:
    """Return yfinance sector string for ticker, or None on failure."""
    yahoo_sym = YAHOO_ALIAS.get(ticker, ticker)
    try:
        return yf.Ticker(yahoo_sym).info.get("sector")
    except Exception as exc:
        logger.debug("get_stock_sector %s: %s", ticker, exc)
    return None


def get_stock_sector_etf(ticker: str) -> str | None:
    """Return SPDR sector ETF symbol for ticker, or None if unmapped."""
    sector = get_stock_sector(ticker)
    if sector:
        return SECTOR_ETF_MAP.get(sector)
    return None


# ---------------------------------------------------------------------------
# Relative-strength ranking within sector
# ---------------------------------------------------------------------------

def _fetch_return(ticker: str, lookback: int) -> tuple[str, float | None]:
    yahoo_sym = YAHOO_ALIAS.get(ticker, ticker)
    try:
        df = yf.Ticker(yahoo_sym).history(period=f"{lookback + 25}d", auto_adjust=True)
        if len(df) < lookback:
            return ticker, None
        old_px = float(df["Close"].iloc[-lookback])
        new_px = float(df["Close"].iloc[-1])
        if old_px <= 0:
            return ticker, None
        return ticker, (new_px - old_px) / old_px
    except Exception as exc:
        logger.debug("_fetch_return %s: %s", ticker, exc)
        return ticker, None


def build_sector_returns(sector_etf: str, lookback: int = 63) -> dict[str, float]:
    """
    Compute lookback-day returns for all SECTOR_PEERS entries for sector_etf.
    Returns {ticker: return} for tickers with sufficient history.
    Uses ThreadPoolExecutor (max 15 workers).
    """
    peers = SECTOR_PEERS.get(sector_etf, [])
    if not peers:
        logger.debug("build_sector_returns: no peers defined for %s", sector_etf)
        return {}

    returns: dict[str, float] = {}
    with ThreadPoolExecutor(max_workers=15) as ex:
        futures = {ex.submit(_fetch_return, t, lookback): t for t in peers}
        for f in as_completed(futures):
            ticker, ret = f.result()
            if ret is not None:
                returns[ticker] = ret

    logger.debug("build_sector_returns %s: %d/%d peers", sector_etf, len(returns), len(peers))
    return returns


def compute_sector_rs_rank(ticker: str, sector_returns: dict[str, float]) -> int | None:
    """
    Rank ticker by 63-day return within sector_returns (1 = best performer).
    Returns None if ticker is absent from sector_returns.
    """
    if ticker not in sector_returns:
        return None
    ranked = sorted(sector_returns, key=lambda t: sector_returns[t], reverse=True)
    return ranked.index(ticker) + 1
