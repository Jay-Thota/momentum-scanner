import logging
import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

from backend import config

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")

# Yahoo Finance uses hyphens where standard tickers use dots.
YAHOO_ALIAS: dict[str, str] = {
    "BRK.B": "BRK-B",
    "BF.B":  "BF-B",
}


def _compute_obv(df: pd.DataFrame) -> pd.Series:
    """Standard OBV: +volume on up-close days, -volume on down-close days."""
    close = df["Close"]
    volume = df["Volume"]
    direction = pd.Series(0.0, index=df.index)
    direction[close > close.shift(1)] = 1.0
    direction[close < close.shift(1)] = -1.0
    return (direction * volume).cumsum()


def fetch_daily_ohlcv(ticker: str, _max_retries: int = 2) -> pd.DataFrame:
    """
    Fetch 1 year of daily OHLCV from yfinance (adjusted closes) and append OBV.
    Retries up to _max_retries times with exponential backoff on rate limiting.
    Returns an empty DataFrame on failure.
    """
    yahoo_sym = YAHOO_ALIAS.get(ticker, ticker)
    for attempt in range(_max_retries + 1):
        try:
            df = yf.Ticker(yahoo_sym).history(period="1y", auto_adjust=True)
            if df.empty:
                logger.warning("No OHLCV data returned for %s", ticker)
                return pd.DataFrame()
            df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
            df.sort_index(inplace=True)
            df["OBV"] = _compute_obv(df)
            return df
        except Exception as exc:
            if attempt < _max_retries and "Too Many Requests" in str(exc):
                _time.sleep(2.0 ** attempt)
                continue
            logger.warning("fetch_daily_ohlcv failed for %s: %s", ticker, exc)
            return pd.DataFrame()


def fetch_intraday_block_trades(ticker: str, min_size: int | None = None) -> int:
    """
    Count today's intraday trades with size >= min_size using the Alpaca IEX feed.
    Returns 0 (and logs debug) if API keys are absent or any error occurs.
    """
    if min_size is None:
        min_size = config.BLOCK_TRADE_MIN_SIZE

    if not (config.ALPACA_API_KEY and config.ALPACA_SECRET_KEY):
        logger.debug("Alpaca keys not configured -- skipping block trades for %s", ticker)
        return 0

    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockTradesRequest

        client = StockHistoricalDataClient(
            api_key=config.ALPACA_API_KEY,
            secret_key=config.ALPACA_SECRET_KEY,
        )

        today_et = datetime.now(_ET).date()
        market_open_et = datetime.combine(today_et, time(9, 30), tzinfo=_ET)
        now_utc = datetime.now(timezone.utc)

        req = StockTradesRequest(
            symbol_or_symbols=ticker,
            start=market_open_et.astimezone(timezone.utc),
            end=now_utc,
            feed="iex",
        )
        trades = client.get_stock_trades(req)

        count = 0
        for sym_trades in trades.values():
            for trade in sym_trades:
                if trade.size >= min_size:
                    count += 1

        logger.debug("Block trades for %s (min_size=%d): %d", ticker, min_size, count)
        return count

    except Exception as exc:
        logger.debug("Block trade fetch failed for %s: %s", ticker, exc)
        return 0


def bars_to_frame(bar_list: list) -> pd.DataFrame:
    """One ticker's Alpaca bars -> OHLCV DataFrame with OBV (empty if no bars)."""
    if not bar_list:
        return pd.DataFrame()
    df = pd.DataFrame({
        "Open":   [float(b.open)   for b in bar_list],
        "High":   [float(b.high)   for b in bar_list],
        "Low":    [float(b.low)    for b in bar_list],
        "Close":  [float(b.close)  for b in bar_list],
        # IEX captures ~2-3% of consolidated tape; scale up so that
        # volume thresholds (MIN_AVG_VOLUME, RVOL) remain calibrated.
        "Volume": [float(b.volume) * config.ALPACA_IEX_VOLUME_MULTIPLIER
                   for b in bar_list],
    })
    df.index = pd.DatetimeIndex([b.timestamp for b in bar_list])
    df.sort_index(inplace=True)
    df["OBV"] = _compute_obv(df)
    return df


def fetch_daily_ohlcv_batch_alpaca(
    tickers: list[str],
    lookback_days: int = 300,
    chunk_size: int = 200,
) -> dict[str, pd.DataFrame]:
    """
    Batch-fetch daily OHLCV from Alpaca IEX for the full universe in a small
    number of API calls.  Each ticker's DataFrame includes the OBV column so
    Layers 1 and 2 can run immediately without any additional Yahoo calls.

    Returns {ticker: DataFrame}.  DataFrame is empty for tickers Alpaca did not
    return data for -- the caller should fall back to Yahoo for those.
    Returns an empty dict if Alpaca is not configured.
    """
    bars = fetch_daily_bars_alpaca(tickers, lookback_days, chunk_size)
    return {t: bars_to_frame(b) for t, b in bars.items()}


def fetch_daily_bars_alpaca(
    tickers: list[str],
    lookback_days: int = 300,
    chunk_size: int = 200,
) -> dict[str, list]:
    """
    The raw Alpaca IEX daily bars behind fetch_daily_ohlcv_batch_alpaca:
    {ticker: [Bar, ...]}, an empty list for tickers with no data or in a
    failed chunk. Returns an empty dict if Alpaca is not configured.
    """
    if not (config.ALPACA_API_KEY and config.ALPACA_SECRET_KEY):
        logger.info("Alpaca keys not configured -- batch OHLCV unavailable")
        return {}

    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
    except ImportError:
        logger.warning("alpaca-py not installed -- batch OHLCV unavailable")
        return {}

    client   = StockHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY)
    start_dt = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    end_dt   = datetime.now(timezone.utc)

    result: dict[str, list] = {}

    for offset in range(0, len(tickers), chunk_size):
        chunk = tickers[offset : offset + chunk_size]
        try:
            req = StockBarsRequest(
                symbol_or_symbols=chunk,
                timeframe=TimeFrame.Day,
                start=start_dt,
                end=end_dt,
                feed="iex",
            )
            bars = client.get_stock_bars(req)

            for ticker in chunk:
                result[ticker] = bars.data.get(ticker) or []

            have = sum(1 for t in chunk if result[t])
            logger.info(
                "Alpaca batch %d-%d: %d/%d tickers returned data",
                offset, offset + len(chunk), have, len(chunk),
            )

        except Exception as exc:
            logger.warning(
                "Alpaca batch chunk %d-%d failed: %s -- marking as empty",
                offset, offset + len(chunk), exc,
            )
            for t in chunk:
                if t not in result:
                    result[t] = []

    return result


def fetch_batch(tickers: list[str], workers: int = 15) -> dict[str, pd.DataFrame]:
    """
    Fetch 1yr daily OHLCV + OBV for multiple tickers in parallel.
    Returns {ticker: DataFrame}; DataFrame is empty on any failure.
    """
    results: dict[str, pd.DataFrame] = {}
    n = min(workers, len(tickers)) if tickers else 1
    with ThreadPoolExecutor(max_workers=n) as ex:
        futures = {ex.submit(fetch_daily_ohlcv, t): t for t in tickers}
        for f in as_completed(futures):
            t = futures[f]
            try:
                results[t] = f.result()
            except Exception as exc:
                logger.warning("fetch_batch failed for %s: %s", t, exc)
                results[t] = pd.DataFrame()
    return results
