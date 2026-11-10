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

Run with:
    python -m backend.tests.validate_security_types
"""

import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

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

    print(f"\n{SEP}\n  {'ALL PASSED' if ok else 'FAILURES ABOVE'}\n{SEP}\n")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
