"""
backend/data/security_types.py

Which symbols are exchange-traded funds, from the Nasdaq Trader symbol
directory: the exchanges' own daily lists of every listed symbol, each with
an ETF flag.

  nasdaqlisted.txt  Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares
  otherlisted.txt   ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol

Both end with a "File Creation Time: ..." line.

The flag marks ETFs and exchange-traded trusts (SGOV, QQQ, GLD, USO, GBTC).
It does not mark closed-end funds (PDI, NAD), and ETNs are inconsistent
(AMJB is flagged, VXX is not). On the June 2026 history universe it agrees
with yfinance quoteType on every flagged symbol.

Used by the universe refresh (no Yahoo calls there) and by the backtest.

Public API:
    etf_symbols(cache_dir=None) -> frozenset[str]
"""

import logging
import time
from pathlib import Path

import requests

from backend import config

logger = logging.getLogger(__name__)

_BASE = "https://www.nasdaqtrader.com/dynamic/SymDir/"
# file name -> (symbol column, ETF column)
_FILES = {
    "nasdaqlisted.txt": ("Symbol", "ETF"),
    "otherlisted.txt": ("ACT Symbol", "ETF"),
}
# Far below the ~4,000+ ETFs the files list: fewer means a format change or
# a truncated download, not a real answer
_MIN_ETFS = 1000


def parse_directory(text: str, symbol_col: str, etf_col: str) -> dict[str, bool]:
    """{symbol: is_etf} from one pipe-delimited directory file."""
    lines = [ln for ln in text.splitlines() if ln and not ln.startswith("File Creation Time")]
    if not lines:
        return {}
    header = lines[0].split("|")
    try:
        si, ei = header.index(symbol_col), header.index(etf_col)
    except ValueError as exc:
        raise ValueError(f"symbol directory header changed: {lines[0]!r}") from exc
    out: dict[str, bool] = {}
    for ln in lines[1:]:
        cols = ln.split("|")
        if len(cols) > max(si, ei) and cols[si]:
            out[cols[si]] = cols[ei] == "Y"
    return out


def _read_or_download(name: str, cache_dir: Path) -> str:
    """The cached file if younger than SYMDIR_MAX_AGE_DAYS, else a fresh download
    (falling back to an older cached copy if the download fails)."""
    path = cache_dir / name
    age_days = (time.time() - path.stat().st_mtime) / 86400 if path.exists() else None
    if age_days is not None and age_days < config.SYMDIR_MAX_AGE_DAYS:
        return path.read_text(encoding="utf-8")
    try:
        resp = requests.get(_BASE + name, timeout=30)
        resp.raise_for_status()
        cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(resp.text, encoding="utf-8")
        logger.info("Downloaded symbol directory %s", name)
        return resp.text
    except Exception as exc:
        if age_days is None:
            raise RuntimeError(f"symbol directory {name} unavailable and not cached: {exc}") from exc
        logger.warning("Symbol directory %s download failed (%s); using the %.0f-day-old copy",
                       name, exc, age_days)
        return path.read_text(encoding="utf-8")


def etf_symbols(cache_dir: str | Path | None = None) -> frozenset[str]:
    """
    Symbols the exchanges flag as ETFs. Raises RuntimeError if the directory
    can't be downloaded or read (callers should fail rather than silently
    let funds back in).
    """
    cache = Path(cache_dir or config.SYMDIR_CACHE_DIR)
    flags: dict[str, bool] = {}
    for name, (sym_col, etf_col) in _FILES.items():
        flags.update(parse_directory(_read_or_download(name, cache), sym_col, etf_col))
    etfs = frozenset(s for s, is_etf in flags.items() if is_etf)
    if len(etfs) < _MIN_ETFS:
        raise RuntimeError(f"symbol directory lists only {len(etfs)} ETFs; expected thousands")
    return etfs
