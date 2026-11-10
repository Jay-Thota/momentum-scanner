"""
backend/tests/validate_security_types.py

Offline checks of backend/data/security_types.py (no network: requests.get
is mocked, the cache is a temp directory):
  - Both directory formats parse, the footer line is ignored, and the ETF
    flag is read from the right column
  - A changed header raises instead of misreading columns
  - A fresh cache is used without a download; a stale one is re-downloaded
  - A failed download falls back to the cached copy, or raises with no cache
  - A directory with implausibly few ETFs raises

and that the filter only narrows the pickable universe, never what is
fetched for the regime filter, L5 or the backtest benchmark, even if SPY and
every sector ETF are flagged:
  - the scan's sector pre-fetch still requests all 11 sector ETFs
  - the regime filter reads ^GSPC directly, not the universe
  - export_history still puts SPY and the sector ETFs in history.bin
  - (with engine/data/history.bin) excluding every flagged ETF and SPY from
    the picks leaves the SPY regime and the benchmark returns unchanged

Run with:
    python -m backend.tests.validate_security_types
"""

import os
import sys
import tempfile
import time
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

from backend.data import security_types as st

SEP = "=" * 68

# Lines copied from the live files (2026-11-10), plus filler ETFs so the
# sanity floor passes
NASDAQ = (
    "Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares\n"
    "AAPL|Apple Inc. - Common Stock|Q|N|N|100|N|N\n"
    "QQQ|Invesco QQQ Trust, Series 1|G|N|N|100|Y|N\n"
    "TBIL|F/m US Treasury 3 Month Bill Fund|G|N|N|100|Y|N\n"
    + "".join(f"QF{i}|Filler ETF {i}|G|N|N|100|Y|N\n" for i in range(600))
    + "File Creation Time: 1110202617:01|||||||\n"
)
OTHER = (
    "ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol\n"
    "A|Agilent Technologies, Inc. Common Stock|N|A|N|100|N|A\n"
    "PDI|PIMCO Dynamic Income Fund Common Stock|N|PDI|N|100|N|PDI\n"
    "SGOV|iShares 0-3 Month Treasury Bond ETF|N|SGOV|Y|100|N|SGOV\n"
    "GLD|SPDR Gold Shares|P|GLD|Y|100|N|GLD\n"
    + "".join(f"OF{i}|Filler ETF {i}|P|OF{i}|Y|100|N|OF{i}\n" for i in range(600))
    + "File Creation Time: 1110202617:01||||||\n"
)
FILES = {"nasdaqlisted.txt": NASDAQ, "otherlisted.txt": OTHER}


def _pf(cond: bool, label: str, detail: str = "") -> bool:
    print(f"  {'[PASS]' if cond else '[FAIL]'}  {label}" + (f"  ({detail})" if detail else ""))
    return cond


def _get(files=FILES, fail=False):
    def get(url, timeout):
        if fail:
            raise ConnectionError("simulated outage")
        resp = MagicMock()
        resp.text = files[url.rsplit("/", 1)[1]]
        return resp
    return MagicMock(side_effect=get)


def main() -> None:
    print(f"\n{SEP}\n  Symbol directory ETF flags (offline)\n{SEP}")
    ok = True

    flags = st.parse_directory(OTHER, "ACT Symbol", "ETF")
    ok &= _pf(flags["SGOV"] and flags["GLD"] and not flags["PDI"] and not flags["A"],
              "otherlisted: ETF column read", "SGOV, GLD flagged; PDI (closed-end fund), A not")
    ok &= _pf(not any(k.startswith("File Creation") for k in flags), "footer ignored")
    flags = st.parse_directory(NASDAQ, "Symbol", "ETF")
    ok &= _pf(flags["QQQ"] and flags["TBIL"] and not flags["AAPL"], "nasdaqlisted: ETF column read")
    try:
        st.parse_directory(NASDAQ.replace("|ETF|", "|IsETF|"), "Symbol", "ETF")
        ok &= _pf(False, "changed header raises")
    except ValueError:
        ok &= _pf(True, "changed header raises")

    with tempfile.TemporaryDirectory() as tmp, \
         patch.object(st.config, "SYMDIR_MAX_AGE_DAYS", 1.0):
        get = _get()
        with patch.object(st.requests, "get", get):
            etfs = st.etf_symbols(tmp)
        ok &= _pf({"QQQ", "TBIL", "SGOV", "GLD"} <= etfs and not {"AAPL", "A", "PDI"} & etfs,
                  "first call downloads both files", f"{get.call_count} downloads, {len(etfs)} ETFs")

        get = _get()
        with patch.object(st.requests, "get", get):
            st.etf_symbols(tmp)
        ok &= _pf(get.call_count == 0, "fresh cache: no download")

        old = time.time() - 2 * 86400
        for name in FILES:
            os.utime(Path(tmp) / name, (old, old))
        get = _get()
        with patch.object(st.requests, "get", get):
            st.etf_symbols(tmp)
        ok &= _pf(get.call_count == 2, "stale cache: re-downloaded", f"{get.call_count} downloads")

        for name in FILES:
            os.utime(Path(tmp) / name, (old, old))
        with patch.object(st.requests, "get", _get(fail=True)):
            etfs = st.etf_symbols(tmp)
        ok &= _pf("SGOV" in etfs, "download fails: older cached copy used")

    with tempfile.TemporaryDirectory() as tmp:
        try:
            with patch.object(st.requests, "get", _get(fail=True)):
                st.etf_symbols(tmp)
            ok &= _pf(False, "download fails with no cache: raises")
        except RuntimeError as exc:
            ok &= _pf(True, "download fails with no cache: raises", str(exc)[:60])

    with tempfile.TemporaryDirectory() as tmp:
        # header + first three rows: a handful of ETFs, no filler
        tiny = {name: "\n".join(text.splitlines()[:4]) + "\n" for name, text in FILES.items()}
        try:
            with patch.object(st.requests, "get", _get(files=tiny)):
                st.etf_symbols(tmp)
            ok &= _pf(False, "implausibly few ETFs: raises")
        except RuntimeError as exc:
            ok &= _pf(True, "implausibly few ETFs: raises", str(exc))

    ok &= check_data_fetches_unaffected()

    print(f"\n{SEP}\n  {'ALL PASSED' if ok else 'FAILURES ABOVE'}\n{SEP}\n")
    sys.exit(0 if ok else 1)


def check_data_fetches_unaffected() -> bool:
    from backend.data.fetcher_sector import SECTOR_PEERS
    from backend.scoring import scorer
    from backend.utils import regime_filter
    from scripts import export_history

    print("\n  The filter narrows the pickable universe only")
    ok = True
    sector_etfs = set(SECTOR_PEERS)
    flagged = frozenset({"SPY", *sector_etfs})   # worst case: all of them flagged

    # Scan Step 3: the sector pre-fetch iterates SECTOR_PEERS, not the universe
    calls: dict[str, set[str]] = {"etf": set(), "ohlcv": set(), "sr": set()}
    with patch("backend.universe.etf_symbols", return_value=flagged), \
         patch.object(scorer, "fetch_sector_data", side_effect=lambda e: calls["etf"].add(e) or {}), \
         patch.object(scorer, "fetch_daily_ohlcv", side_effect=lambda e: calls["ohlcv"].add(e)), \
         patch.object(scorer, "build_sector_returns", side_effect=lambda e: calls["sr"].add(e) or {}):
        scorer._prefetch_sector_data()
    ok &= _pf(all(c == sector_etfs for c in calls.values()),
              "scan's sector pre-fetch requests all 11 sector ETFs (L5)",
              ", ".join(f"{k}={len(v)}" for k, v in calls.items()))

    # Regime: ^GSPC from yfinance; the module never sees the universe
    asked: list[str] = []

    def ticker(sym):
        asked.append(sym)
        raise ConnectionError("offline")

    with patch.object(regime_filter.yf, "Ticker", side_effect=ticker):
        regime_filter.get_regime(force_refresh=True)
    uses_universe = any(hasattr(regime_filter, n) for n in ("get_universe", "etf_symbols"))
    ok &= _pf(asked[:1] == ["^GSPC"] and not uses_universe,
              "regime filter reads ^GSPC, not the universe", f"requested {sorted(set(asked))}")

    # history.bin: SPY and the sector ETFs are added by name, ahead of the universe
    captured: list[str] = []

    def stop(order, start, end):
        captured.extend(order)
        raise StopIteration

    with patch.object(export_history, "load_universe", return_value=["AAPL", "MSFT"]), \
         patch.object(export_history, "trading_calendar", side_effect=pd.bdate_range), \
         patch.object(export_history, "fetch_history", side_effect=stop):
        try:
            export_history.build_panel(date(2026, 6, 1), 1.0)
        except StopIteration:
            pass
    ok &= _pf(captured[:1 + len(sector_etfs)] == ["SPY", *sorted(sector_etfs)]
              and captured[1 + len(sector_etfs):] == ["AAPL", "MSFT"],
              "export_history fetches SPY + sector ETFs even when the universe has none",
              " ".join(captured))

    # Backtest: excluding names only clears their pick eligibility
    history = Path(__file__).resolve().parents[2] / "engine" / "data" / "history.bin"
    try:
        import mscan_engine
    except ImportError:
        mscan_engine = None
    if mscan_engine is None or not history.exists():
        print("  [SKIP]  backtest regime with SPY excluded (needs mscan_engine and history.bin)")
        return ok
    from backend.scoring.cpp_engine import engine_config
    from scripts.read_history_bin import read_history_bin

    symbols = [str(s) for s in read_history_bin(history)["symbols"]]
    in_file_etfs = sorted(st.etf_symbols() & set(symbols))
    run = lambda exclude: mscan_engine.run_backtest(   # noqa: E731
        str(history), engine_config(), start=int(read_history_bin(history)["dates"][252]),
        exclude=exclude, horizons=[5])
    base = run(sorted(sector_etfs))
    filtered = run(sorted({"SPY", *sector_etfs, *in_file_etfs}))
    picked = {p["ticker"] for p in filtered["picks"]}
    ok &= _pf(filtered["bear_days"] == base["bear_days"] and filtered["num_tickers"] == len(symbols),
              "backtest: SPY regime unchanged with SPY and all ETFs excluded from picks",
              f"bear days {base['bear_days']} vs {filtered['bear_days']}, "
              f"{filtered['num_tickers']} tickers loaded")
    # SPY's forward return per pick day = return - excess; same days -> same values
    days_b = {p["date"]: p["fwd_returns"][0] - p["excess_vs_spy"][0] for p in base["picks"]
              if p["fwd_returns"][0] is not None}
    same = all(abs(days_b[p["date"]] - (p["fwd_returns"][0] - p["excess_vs_spy"][0])) < 1e-12
               for p in filtered["picks"] if p["fwd_returns"][0] is not None and p["date"] in days_b)
    ok &= _pf(same, "backtest: SPY benchmark returns unchanged")
    ok &= _pf(not picked & {"SPY", *sector_etfs, *in_file_etfs},
              "backtest: no excluded symbol picked", f"{len(in_file_etfs)} flagged ETFs in the file")
    return ok


if __name__ == "__main__":
    main()
