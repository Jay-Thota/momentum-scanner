"""
backend/data/fetcher_options.py

Options flow signals from yfinance (nearest 2 expirations) and
intraday VWAP from Alpaca IEX minute bars.
Put/call ratios are persisted to the shared SQLAlchemy DB for 5-day averaging.
"""

import logging
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf
from sqlalchemy import select

from backend import config
from backend.db.database import SessionLocal, init_db
from backend.db.models import OptionsHistory

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")

YAHOO_ALIAS: dict[str, str] = {"BRK.B": "BRK-B", "BF.B": "BF-B"}

# Ensure the options_history table exists on first import.
init_db()


# ---------------------------------------------------------------------------
# ORM helpers
# ---------------------------------------------------------------------------

def _get_5d_avg_pc(ticker: str, today: str) -> float | None:
    """Return mean pc_ratio from the 5 most recent scans before today."""
    with SessionLocal() as session:
        rows = (
            session.execute(
                select(OptionsHistory.put_call_ratio)
                .where(OptionsHistory.ticker == ticker)
                .where(OptionsHistory.scan_date < today)
                .where(OptionsHistory.put_call_ratio.is_not(None))
                .order_by(OptionsHistory.scan_date.desc())
                .limit(5)
            )
            .scalars()
            .all()
        )
    return float(sum(rows) / len(rows)) if rows else None


def _upsert_options(
    ticker: str,
    today: str,
    pc_ratio: float | None,
    cvor: float | None,
    iv_slope: float | None,
) -> None:
    """Insert or update today's options snapshot for ticker."""
    with SessionLocal() as session:
        row = session.execute(
            select(OptionsHistory)
            .where(OptionsHistory.ticker == ticker)
            .where(OptionsHistory.scan_date == today)
        ).scalar_one_or_none()

        if row is None:
            row = OptionsHistory(ticker=ticker, scan_date=today)
            session.add(row)

        row.put_call_ratio    = pc_ratio
        row.call_vol_oi_ratio = cvor
        row.iv_slope          = iv_slope
        session.commit()


# ---------------------------------------------------------------------------
# IV helper -- average ATM-ish IV from an options chain DataFrame
# ---------------------------------------------------------------------------

def _avg_iv(calls_df: pd.DataFrame) -> float | None:
    """
    Mean implied volatility of calls with volume > 0 and IV > 0.
    Returns None if no qualifying contracts.
    """
    active = calls_df[
        (calls_df["volume"].fillna(0) > 0)
        & (calls_df["impliedVolatility"].fillna(0) > 0)
    ]
    if active.empty:
        return None
    val = active["impliedVolatility"].mean()
    return float(val) if not pd.isna(val) else None


# ---------------------------------------------------------------------------
# Public: options signals
# ---------------------------------------------------------------------------

def fetch_options_signals(ticker: str) -> dict:
    """
    Pull the nearest two expiry chains and compute:
        put_call_ratio       -- front-expiry put vol / call vol
        call_vol_oi_ratio    -- front-expiry call vol / call OI
        iv_slope             -- front_avg_IV - next_avg_IV
        pc_ratio_5d_avg      -- 5-session rolling average from DB

    Always sets options_partial=True (sweeps / dark-pool unavailable on free tier).
    Returns a partially-populated dict on any error rather than raising.
    """
    today = date.today().isoformat()
    out: dict = {
        "put_call_ratio":         None,
        "call_vol_oi_ratio":      None,
        "iv_slope":               None,
        "pc_ratio_5d_avg":        None,
        "avg_daily_option_volume": None,
        "options_partial":        True,
    }

    yahoo_sym = YAHOO_ALIAS.get(ticker, ticker)
    try:
        t           = yf.Ticker(yahoo_sym)
        expirations = t.options
        if not expirations:
            logger.info("L3 [%s] fetch_options_signals => EMPTY (no option expirations from yfinance)", ticker)
            return out

        # -- Front expiry chain --
        chain1 = t.option_chain(expirations[0])
        calls1 = chain1.calls.copy()
        puts1  = chain1.puts.copy()

        for df in (calls1, puts1):
            df["volume"]            = df["volume"].fillna(0)
            df["openInterest"]      = df["openInterest"].fillna(0)
            df["impliedVolatility"] = df["impliedVolatility"].fillna(0)

        call_vol = float(calls1["volume"].sum())
        put_vol  = float(puts1["volume"].sum())
        call_oi  = float(calls1["openInterest"].sum())

        adov = call_vol + put_vol  # avg_daily_option_volume proxy
        logger.info(
            "L3 [%s] raw chain (%s): call_vol=%.0f  put_vol=%.0f  call_oi=%.0f  "
            "adov=%.0f  expiries_available=%d",
            ticker, expirations[0], call_vol, put_vol, call_oi, adov, len(expirations),
        )

        pc_ratio = (put_vol / call_vol) if call_vol > 0 else None
        cvor     = (call_vol / call_oi) if call_oi  > 0 else None

        if pc_ratio is None:
            logger.info("L3 [%s] put_call_ratio=None (call_vol=0 -- division skipped)", ticker)
        if cvor is None:
            logger.info("L3 [%s] call_vol_oi_ratio=None (call_oi=0 -- division skipped)", ticker)

        # -- IV slope: front minus next (active calls only) --
        iv_slope: float | None = None
        iv1 = _avg_iv(calls1)
        if iv1 is not None and len(expirations) > 1:
            chain2 = t.option_chain(expirations[1])
            calls2 = chain2.calls.copy()
            calls2["volume"]            = calls2["volume"].fillna(0)
            calls2["impliedVolatility"] = calls2["impliedVolatility"].fillna(0)
            iv2 = _avg_iv(calls2)
            if iv2 is not None:
                iv_slope = round(iv1 - iv2, 6)
            logger.info("L3 [%s] IV: front_avg=%.4f  next_avg=%s  iv_slope=%s",
                        ticker, iv1,
                        f"{iv2:.4f}" if iv2 is not None else "None",
                        f"{iv_slope:.6f}" if iv_slope is not None else "None")
        else:
            logger.info("L3 [%s] IV: front_avg=%s  iv_slope=None (%s)",
                        ticker,
                        f"{iv1:.4f}" if iv1 is not None else "None",
                        "no second expiry" if iv1 is not None else "no active calls in front expiry")

        out["put_call_ratio"]          = round(pc_ratio, 4) if pc_ratio is not None else None
        out["call_vol_oi_ratio"]       = round(cvor, 4)     if cvor    is not None else None
        out["iv_slope"]                = iv_slope
        out["avg_daily_option_volume"] = int(adov)

    except Exception as exc:
        logger.warning("L3 [%s] fetch_options_signals => EXCEPTION: %s", ticker, exc)
        return out

    # -- 5-day rolling average (SELECT only -- batch upsert handled by caller) --
    try:
        out["pc_ratio_5d_avg"] = _get_5d_avg_pc(ticker, today)
    except Exception as exc:
        logger.warning("DB pc_ratio read error for %s: %s", ticker, exc)

    populated = any(v is not None for k, v in out.items() if k != "options_partial")
    logger.info(
        "L3 [%s] fetch_options_signals => %s | "
        "pcr=%s  cvor=%s  iv_slope=%s  pc_ratio_5d_avg=%s  adov=%s",
        ticker,
        "POPULATED" if populated else "ALL-NONE",
        f"{out['put_call_ratio']:.4f}"    if out["put_call_ratio"]    is not None else "None",
        f"{out['call_vol_oi_ratio']:.4f}" if out["call_vol_oi_ratio"] is not None else "None",
        f"{out['iv_slope']:.6f}"          if out["iv_slope"]          is not None else "None",
        f"{out['pc_ratio_5d_avg']:.4f}"   if out["pc_ratio_5d_avg"]   is not None else "None",
        out["avg_daily_option_volume"],
    )
    return out


# ---------------------------------------------------------------------------
# Public: intraday VWAP from Alpaca IEX minute bars
# ---------------------------------------------------------------------------

def fetch_vwap(ticker: str) -> float | None:
    """
    Compute today's session VWAP from Alpaca IEX bars (minute, with hour fallback).
    Caps the request at 4:00 PM ET so post-market bars are never included.
    Returns None when API keys are absent, market hasn't opened, or any error occurs.

    Root cause note: BarSet has no .get() method -- must use bars_resp.data.get(ticker).
    """
    if not (config.ALPACA_API_KEY and config.ALPACA_SECRET_KEY):
        logger.debug("Alpaca keys not configured -- VWAP unavailable for %s", ticker)
        return None

    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        now_et       = datetime.now(_ET)
        today_et     = now_et.date()
        market_open  = datetime.combine(today_et, time(9, 30), tzinfo=_ET)
        market_close = datetime.combine(today_et, time(16, 0),  tzinfo=_ET)

        if now_et < market_open:
            logger.debug("Pre-market -- VWAP unavailable for %s", ticker)
            return None

        # Cap end at market close so post-market bars never enter the VWAP calc.
        end_et    = min(now_et, market_close)
        start_utc = market_open.astimezone(timezone.utc)
        end_utc   = end_et.astimezone(timezone.utc)

        logger.info(
            "VWAP [%s] requesting bars  start=%s  end=%s  (ET: %s -> %s)",
            ticker,
            start_utc.isoformat(), end_utc.isoformat(),
            market_open.strftime("%H:%M"), end_et.strftime("%H:%M"),
        )

        client = StockHistoricalDataClient(
            api_key=config.ALPACA_API_KEY,
            secret_key=config.ALPACA_SECRET_KEY,
        )

        def _vwap_from_bars(bars: list) -> float | None:
            total_vol = sum(b.volume for b in bars)
            if total_vol == 0:
                return None
            total_tp_vol = sum((b.high + b.low + b.close) / 3.0 * b.volume for b in bars)
            return float(total_tp_vol / total_vol)

        # -- Attempt 1: 1-minute bars -----------------------------------------
        req = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=TimeFrame.Minute,
            start=start_utc,
            end=end_utc,
            feed="iex",
        )
        bars_resp = client.get_stock_bars(req)
        # BarSet exposes data via .data dict -- .get() is NOT on BarSet itself.
        sym_bars = list(bars_resp.data.get(ticker) or [])

        if sym_bars:
            vwap = _vwap_from_bars(sym_bars)
            logger.info(
                "VWAP [%s] minute bars=%d  first=%s  last=%s  vwap=%.4f",
                ticker, len(sym_bars),
                sym_bars[0].timestamp.strftime("%H:%M"),
                sym_bars[-1].timestamp.strftime("%H:%M"),
                vwap or 0,
            )
            return vwap

        # -- Attempt 2: hourly fallback (e.g. thin IEX coverage) ---------------
        logger.info("VWAP [%s] minute bars empty -- trying hourly fallback", ticker)
        req_h = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=TimeFrame.Hour,
            start=start_utc,
            end=end_utc,
            feed="iex",
        )
        bars_h = client.get_stock_bars(req_h)
        sym_bars_h = list(bars_h.data.get(ticker) or [])

        if sym_bars_h:
            vwap = _vwap_from_bars(sym_bars_h)
            logger.info(
                "VWAP [%s] hourly bars=%d  vwap=%.4f  (fallback)",
                ticker, len(sym_bars_h), vwap or 0,
            )
            return vwap

        logger.info("VWAP [%s] no bars returned from either timeframe", ticker)
        return None

    except Exception as exc:
        logger.warning("fetch_vwap failed for %s: %s", ticker, exc)
        return None


# ---------------------------------------------------------------------------
# Public: batch OptionsHistory upsert (single session, called after parallel fetch)
# ---------------------------------------------------------------------------

def batch_upsert_options(records: list[dict]) -> None:
    """
    Write all OptionsHistory rows in a single SQLAlchemy session.
    Avoids concurrent SQLite write contention when called after parallel fetches.

    Each record: {ticker, scan_date, put_call_ratio, cvor, iv_slope}
    """
    if not records:
        return
    with SessionLocal() as session:
        for rec in records:
            ticker    = rec["ticker"]
            scan_date = rec["scan_date"]
            row = session.execute(
                select(OptionsHistory)
                .where(OptionsHistory.ticker    == ticker)
                .where(OptionsHistory.scan_date == scan_date)
            ).scalar_one_or_none()
            if row is None:
                row = OptionsHistory(ticker=ticker, scan_date=scan_date)
                session.add(row)
            row.put_call_ratio    = rec.get("put_call_ratio")
            row.call_vol_oi_ratio = rec.get("cvor")
            row.iv_slope          = rec.get("iv_slope")
        session.commit()
    logger.info("batch_upsert_options: %d rows written", len(records))
