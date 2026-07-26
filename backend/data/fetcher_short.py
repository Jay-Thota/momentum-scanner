"""
backend/data/fetcher_short.py

Short interest data from two sources:
  1. yfinance .info -- shortPercentOfFloat, shortRatio (days-to-cover)
  2. FINRA SSHO daily files -- short volume fraction for trend detection

FINRA daily URL: https://cdn.finra.org/equity/regsho/daily/CNMSshvol{YYYYMMDD}.txt
File is pipe-delimited: Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market
"""

import logging
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

from backend import config

logger = logging.getLogger(__name__)

YAHOO_ALIAS: dict[str, str] = {"BRK.B": "BRK-B", "BF.B": "BF-B"}

_FINRA_BASE = "https://cdn.finra.org/equity/regsho/daily/CNMSshvol{}.txt"
# Rising/declining threshold: 10% change in short-volume fraction
_TREND_THRESHOLD = 0.10


# ---------------------------------------------------------------------------
# yfinance short interest
# ---------------------------------------------------------------------------

def get_short_data(ticker: str, cached_metadata: dict) -> dict:
    """
    Return short interest metrics from a pre-fetched MetadataCache dict.
    No Yahoo call is made -- data was populated during the MetadataCache warm-up.

    Returns:
        {
            "si_pct":        float | None  -- decimal fraction e.g. 0.10 = 10%
            "days_to_cover": float | None  -- shortRatio (days to cover)
        }
    """
    return {
        "si_pct":        cached_metadata.get("si_pct"),
        "days_to_cover": cached_metadata.get("days_to_cover"),
    }


def get_short_data_yfinance(ticker: str) -> dict:
    """
    Direct yfinance fetch for short interest (used by validate scripts only).
    Production code should call get_short_data(ticker, cached_metadata) instead.
    """
    yahoo_sym = YAHOO_ALIAS.get(ticker, ticker)
    out: dict = {"si_pct": None, "days_to_cover": None}
    try:
        info = yf.Ticker(yahoo_sym).info
        si   = info.get("shortPercentOfFloat")
        dtc  = info.get("shortRatio")
        if si  is not None: out["si_pct"]        = float(si)
        if dtc is not None: out["days_to_cover"] = float(dtc)
    except Exception as exc:
        logger.warning("get_short_data_yfinance %s: %s", ticker, exc)
    return out


# ---------------------------------------------------------------------------
# FINRA SSHO helpers
# ---------------------------------------------------------------------------

def _parse_finra_file(path: Path) -> pd.DataFrame:
    try:
        df = pd.read_csv(path, sep="|", dtype={"Symbol": str})
        df = df[df["Symbol"].notna() & (df["Symbol"].str.strip() != "")]
        for col in ("ShortVolume", "ShortExemptVolume", "TotalVolume"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
        return df
    except Exception as exc:
        logger.warning("Failed to parse FINRA file %s: %s", path, exc)
        return pd.DataFrame()


def _get_recent_trading_dates(n: int = 14) -> list[date]:
    """Return the last n calendar days that are plausible trading days (Mon-Fri)."""
    out: list[date] = []
    d = date.today()
    while len(out) < n:
        if d.weekday() < 5:  # Mon-Fri
            out.append(d)
        d -= timedelta(days=1)
    return out  # newest first


def _download_for_date(d: date, cache_dir: Path) -> pd.DataFrame:
    """Download (or return cached) a single FINRA daily file for date d."""
    date_str = d.strftime("%Y%m%d")
    fname    = cache_dir / f"CNMSshvol{date_str}.txt"

    if fname.exists():
        return _parse_finra_file(fname)

    url = _FINRA_BASE.format(date_str)
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code == 200 and len(resp.text) > 200:
            fname.write_text(resp.text, encoding="utf-8")
            logger.info("Downloaded FINRA file %s", fname.name)
            return _parse_finra_file(fname)
        logger.debug("FINRA %s HTTP %s", date_str, resp.status_code)
    except Exception as exc:
        logger.debug("FINRA fetch %s: %s", date_str, exc)
    return pd.DataFrame()


def download_finra_ssho(offset_days: int = 0, cache_dir: str | None = None) -> pd.DataFrame:
    """
    Download the most recent available FINRA consolidated short sale volume file.

    offset_days -- 0 returns the newest available file; positive values return
                   files that many trading-day slots further back. Use offset_days=10
                   to get a file ~2 weeks older for trend comparison.

    Files are cached to FINRA_SHORT_DATA_DIR.
    Returns an empty DataFrame if all attempts fail.
    """
    _dir = Path(cache_dir or config.FINRA_SHORT_DATA_DIR)
    _dir.mkdir(parents=True, exist_ok=True)

    candidates = _get_recent_trading_dates(n=20)  # newest first

    start = offset_days
    for d in candidates[start:]:
        df = _download_for_date(d, _dir)
        if not df.empty:
            return df

    logger.warning(
        "Could not download any FINRA SSHO file (offset_days=%d)", offset_days
    )
    return pd.DataFrame()


# ---------------------------------------------------------------------------
# Trend computation
# ---------------------------------------------------------------------------

def get_short_trend(
    ticker: str,
    current_df: pd.DataFrame,
    prev_df: pd.DataFrame,
) -> str:
    """
    Estimate short-interest trend by comparing ShortVolume/TotalVolume
    fraction between two FINRA daily files.

    Returns "rising", "declining", or "flat".
    Returns "flat" whenever either DataFrame is empty or the ticker is absent.
    """
    if current_df.empty or prev_df.empty:
        return "flat"

    sym      = ticker.upper()
    cur_rows = current_df[current_df["Symbol"] == sym]
    prv_rows = prev_df[prev_df["Symbol"] == sym]

    if cur_rows.empty or prv_rows.empty:
        logger.debug("get_short_trend %s: symbol absent from one FINRA file", ticker)
        return "flat"

    cur_total = float(cur_rows.iloc[0]["TotalVolume"])
    prv_total = float(prv_rows.iloc[0]["TotalVolume"])

    if cur_total == 0 or prv_total == 0:
        return "flat"

    cur_frac = float(cur_rows.iloc[0]["ShortVolume"]) / cur_total
    prv_frac = float(prv_rows.iloc[0]["ShortVolume"]) / prv_total

    if prv_frac == 0:
        return "flat"

    change = (cur_frac - prv_frac) / prv_frac
    if   change >  _TREND_THRESHOLD: return "rising"
    elif change < -_TREND_THRESHOLD: return "declining"
    else:                            return "flat"
