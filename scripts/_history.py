"""
scripts/_history.py

Multi-year daily OHLCV fetch shared by export_history.py and dump_golden.py.

The backend fetchers are tuned for the live scan (~300 calendar days, raw
Alpaca bars, 1y yfinance fallback), so this module mirrors their output shape
(Open/High/Low/Close/Volume/OBV, Alpaca volume scaled by
ALPACA_IEX_VOLUME_MULTIPLIER) but differs in two ways:

  - Alpaca bars are requested with adjustment="all" so multi-year series do not
    contain split cliffs.  This matches yfinance auto_adjust=True.
  - The yfinance fallback takes an explicit start date instead of period="1y".

Every returned DataFrame is indexed by a tz-naive DatetimeIndex of ET session
dates (midnight), sorted, with duplicate dates dropped.
"""

import logging
import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import pandas_market_calendars as mcal
import yfinance as yf

from backend import config
from backend.data.fetcher_price import YAHOO_ALIAS, _compute_obv

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")

SPY = "SPY"

ALPACA_CHUNK_SIZE = 200
ALPACA_WORKERS = 4   # free tier is 200 req/min; each chunk pages through several requests
YAHOO_WORKERS = 5    # same as scorer.py fallback
LATE_START_TOLERANCE = 5  # sessions; see fetch_history


def trading_calendar(start: date, end: date) -> pd.DatetimeIndex:
    """NYSE sessions in [start, end] as tz-naive midnight dates."""
    sched = mcal.get_calendar("NYSE").schedule(start_date=str(start), end_date=str(end))
    return pd.DatetimeIndex(sched.index).tz_localize(None).normalize()


def last_completed_session(now: datetime | None = None) -> date:
    """Most recent NYSE session whose close is at or before now."""
    now = now or datetime.now(timezone.utc)
    sched = mcal.get_calendar("NYSE").schedule(
        start_date=str((now - timedelta(days=14)).date()),
        end_date=str(now.date()),
    )
    done = sched[sched["market_close"] <= pd.Timestamp(now)]
    return done.index[-1].date()


def load_universe() -> list[str]:
    """
    get_universe(), falling back to whatever is in the SQLite cache if the
    Alpaca refresh raises.  (As of alpaca-py 0.43.4, get_all_assets raises a
    ValidationError on assets listed on TXSE, and _fetch_fresh does not catch it.)
    """
    from backend.db.database import SessionLocal, init_db
    from backend.universe import _load_cached, get_universe

    try:
        return get_universe()
    except Exception as exc:
        logger.warning("get_universe() failed (%s) -- reading cached universe directly",
                       str(exc).splitlines()[0])
        init_db()
        with SessionLocal() as db:
            cached = _load_cached(db)
        if not cached:
            raise RuntimeError("universe refresh failed and the cache is empty") from exc
        return cached


def _finish(df: pd.DataFrame) -> pd.DataFrame:
    df = df[~df.index.duplicated(keep="last")].sort_index().copy()
    df["OBV"] = _compute_obv(df)
    return df


def _alpaca_chunk(client, chunk: list[str], start: date, end: date) -> dict[str, pd.DataFrame]:
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    req = StockBarsRequest(
        symbol_or_symbols=chunk,
        timeframe=TimeFrame.Day,
        start=datetime.combine(start, datetime.min.time(), tzinfo=_ET),
        end=datetime.combine(end, datetime.max.time(), tzinfo=_ET),
        feed="iex",
        adjustment="all",
    )
    for attempt in range(3):
        try:
            bars = client.get_stock_bars(req)
            break
        except Exception as exc:
            if attempt == 2:
                raise
            logger.info("Alpaca chunk retry %d (%s): %s", attempt + 1, chunk[0], exc)
            _time.sleep(5.0 * 2 ** attempt)

    out: dict[str, pd.DataFrame] = {}
    for ticker in chunk:
        bar_list = bars.data.get(ticker) or []
        if not bar_list:
            continue
        df = pd.DataFrame({
            "Open":   [float(b.open)  for b in bar_list],
            "High":   [float(b.high)  for b in bar_list],
            "Low":    [float(b.low)   for b in bar_list],
            "Close":  [float(b.close) for b in bar_list],
            "Volume": [float(b.volume) * config.ALPACA_IEX_VOLUME_MULTIPLIER for b in bar_list],
        })
        df.index = pd.DatetimeIndex(
            [b.timestamp.astimezone(_ET).replace(tzinfo=None) for b in bar_list]
        ).normalize()
        out[ticker] = _finish(df)
    return out


def fetch_alpaca(tickers: list[str], start: date, end: date) -> dict[str, pd.DataFrame]:
    """Adjusted Alpaca IEX daily bars.  Tickers with no data are omitted."""
    if not (config.ALPACA_API_KEY and config.ALPACA_SECRET_KEY):
        logger.warning("Alpaca keys not configured -- skipping Alpaca history")
        return {}

    from alpaca.data.historical import StockHistoricalDataClient

    client = StockHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY)
    chunks = [tickers[i:i + ALPACA_CHUNK_SIZE] for i in range(0, len(tickers), ALPACA_CHUNK_SIZE)]

    result: dict[str, pd.DataFrame] = {}
    with ThreadPoolExecutor(max_workers=ALPACA_WORKERS) as ex:
        futs = {ex.submit(_alpaca_chunk, client, c, start, end): c for c in chunks}
        for f in as_completed(futs):
            chunk = futs[f]
            try:
                got = f.result()
                result.update(got)
                logger.info("Alpaca chunk %s..%s: %d/%d", chunk[0], chunk[-1], len(got), len(chunk))
            except Exception as exc:
                logger.warning("Alpaca chunk %s..%s failed: %s", chunk[0], chunk[-1], exc)
    return result


def _yahoo_one(ticker: str, start: date, end: date) -> pd.DataFrame:
    sym = YAHOO_ALIAS.get(ticker, ticker)
    for attempt in range(3):
        try:
            df = yf.Ticker(sym).history(
                start=str(start), end=str(end + timedelta(days=1)), auto_adjust=True,
            )
            break
        except Exception as exc:
            if attempt < 2 and "Too Many Requests" in str(exc):
                _time.sleep(2.0 ** attempt)
                continue
            logger.warning("yfinance history failed for %s: %s", ticker, exc)
            return pd.DataFrame()
    if df.empty:
        return pd.DataFrame()
    df = df[["Open", "High", "Low", "Close", "Volume"]].astype(float)
    df.index = pd.DatetimeIndex(df.index).tz_localize(None).normalize()
    return _finish(df)


def fetch_yahoo(tickers: list[str], start: date, end: date) -> dict[str, pd.DataFrame]:
    """Adjusted yfinance daily bars.  Tickers with no data are omitted."""
    result: dict[str, pd.DataFrame] = {}
    if not tickers:
        return result
    with ThreadPoolExecutor(max_workers=min(YAHOO_WORKERS, len(tickers))) as ex:
        futs = {ex.submit(_yahoo_one, t, start, end): t for t in tickers}
        for f in as_completed(futs):
            df = f.result()
            if not df.empty:
                result[futs[f]] = df
    return result


def fetch_history(
    tickers: list[str], start: date, end: date,
) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
    """
    Alpaca first, yfinance for anything Alpaca did not return.

    Alpaca sometimes starts a long-listed name's history at a recent date
    (e.g. SATS from 2026-10-20, TEVA from 2026-09-14), which would look like an
    IPO once left-padded.  Any ticker whose Alpaca history starts inside the
    window is checked against yfinance; if yfinance reaches back at least
    LATE_START_TOLERANCE sessions earlier, the yfinance series replaces the
    Alpaca one wholesale (no splicing -- the two volume scales differ).

    Returns ({ticker: df}, {ticker: "alpaca" | "yahoo" | "yahoo_longer"}).
    Missing tickers are absent.
    """
    data = fetch_alpaca(tickers, start, end)
    source = {t: "alpaca" for t in data}
    missing = [t for t in tickers if t not in data]
    if missing:
        logger.info("yfinance fallback for %d tickers", len(missing))
        fb = fetch_yahoo(missing, start, end)
        data.update(fb)
        source.update({t: "yahoo" for t in fb})

    cal = trading_calendar(start, end)
    late = [t for t, s in source.items() if s == "alpaca" and data[t].index[0] > cal[0]]
    if late:
        logger.info("Checking %d late-starting Alpaca histories against yfinance", len(late))
        yh = fetch_yahoo(late, start, end)
        for t, ydf in yh.items():
            a_pos = cal.searchsorted(data[t].index[0])
            y_pos = cal.searchsorted(ydf.index[0])
            if a_pos - y_pos >= LATE_START_TOLERANCE:
                logger.info("  %s: Alpaca starts %s, yfinance %s -- using yfinance",
                            t, data[t].index[0].date(), ydf.index[0].date())
                data[t] = ydf
                source[t] = "yahoo_longer"
    # Clip to the window (yfinance end is exclusive, Alpaca may include a partial today)
    lo, hi = pd.Timestamp(start), pd.Timestamp(end)
    data = {
        t: _finish(df.loc[(df.index >= lo) & (df.index <= hi), ["Open", "High", "Low", "Close", "Volume"]])
        for t, df in data.items()
    }
    data = {t: df for t, df in data.items() if not df.empty}
    source = {t: s for t, s in source.items() if t in data}
    return data, source
