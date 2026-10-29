"""
python -m scripts.export_history [--end YYYY-MM-DD] [--years 4] [--panel PATH] [--from-panel]

Pulls ~4 years of adjusted daily OHLCV for the current universe plus SPY and
the 11 sector ETFs (Alpaca IEX, yfinance fallback -- see scripts/_history.py),
aligns every series to one NYSE trading calendar, and NaN-pads days with no bar.

Output: a calendar-aligned panel saved as a compressed .npz:
    dates    int32[D]      YYYYMMDD, NYSE sessions, ascending
    symbols  str[N]        SPY, then sector ETFs (sorted), then universe (sorted)
    kind     int8[N]       0 = benchmark, 1 = sector ETF, 2 = universe equity
    source   str[N]        "alpaca" | "yahoo" | "yahoo_longer"
    first    int32[N]      index into dates of the first real bar
    open, high, low, close, volume   float64[N, D]   NaN where no bar

Days before a symbol's first bar are left-padded NaN.  Missing sessions after
that are also NaN (never forward-filled) and logged in three groups:
  - trailing: the symbol stopped trading before --end (delisted or acquired
    since the universe cache was built)
  - reused symbol: an interior run of >= REUSED_SYMBOL_GAP sessions means two
    different listings share the ticker (BBBY, WLTH, NIQ, ...); everything
    before the gap is blanked so the older listing becomes left padding
  - holes: shorter interior runs (IEX no-trade days in thin names)

The panel is then written as the engine's binary file (--bin), the layout
Universe::open_mmap reads (engine/include/mscan/universe.h):

    offset 0                FileHeader, 40 bytes, little-endian:
                              char[8] magic "MSCANV01", u32 version,
                              u32 num_tickers, u32 num_days, 4 pad bytes,
                              u64 tickers_offset, u64 fields_offset
    offset 40               dates: int32[num_days] YYYYMMDD
    tickers_offset          tickers: num_tickers ASCII names, each NUL-terminated
    (zero padding to a multiple of 8)
    fields_offset           open, high, low, close, volume: each float64
                            [num_tickers * num_days], index ticker * num_days + day

The 4 pad bytes are the ones a C++ compiler inserts before the first uint64,
so sizeof(FileHeader) == 40 on every mainstream ABI.  fields_offset is 8-byte
aligned so the mmapped doubles can be read in place.  The kind/source/first
arrays stay in the .npz only; the engine recomputes first-valid at load.

--from-panel skips the fetch and rebuilds the binary from an existing panel.
scripts/read_history_bin.py round-trips the binary against the panel.
"""

import argparse
import logging
import os
import struct
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from backend.data.fetcher_sector import SECTOR_PEERS
from scripts._history import (
    SPY,
    fetch_history,
    last_completed_session,
    load_universe,
    trading_calendar,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
logger = logging.getLogger("export_history")

FIELDS = ["Open", "High", "Low", "Close", "Volume"]
KIND_BENCHMARK, KIND_SECTOR_ETF, KIND_EQUITY = 0, 1, 2
REUSED_SYMBOL_GAP = 20   # sessions; shortest observed reuse gap was 264, longest hole 5

# Mirrors mscan::FileHeader, including the 4 bytes of padding before tickers_offset
BIN_MAGIC = b"MSCANV01"
BIN_VERSION = 1
BIN_HEADER = struct.Struct("<8sIII4xQQ")
assert BIN_HEADER.size == 40


def write_history_bin(panel: dict[str, np.ndarray], path: Path) -> None:
    """Write the panel in the engine's MSCANV01 format (see module docstring)."""
    symbols = [str(s) for s in panel["symbols"]]
    dates = np.ascontiguousarray(panel["dates"], dtype="<i4")
    n, d = len(symbols), len(dates)

    blob = b"".join(s.encode("ascii") + b"\0" for s in symbols)
    tickers_offset = BIN_HEADER.size + dates.nbytes
    fields_offset = (tickers_offset + len(blob) + 7) // 8 * 8

    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("wb") as fh:
        fh.write(BIN_HEADER.pack(BIN_MAGIC, BIN_VERSION, n, d, tickers_offset, fields_offset))
        fh.write(dates.tobytes())
        fh.write(blob)
        fh.write(b"\0" * (fields_offset - tickers_offset - len(blob)))
        for f in FIELDS:
            arr = np.ascontiguousarray(panel[f.lower()], dtype="<f8")
            if arr.shape != (n, d):
                raise ValueError(f"{f} has shape {arr.shape}, expected {(n, d)}")
            fh.write(arr.tobytes())
    os.replace(tmp, path)   # readers never see a half-written file

    expected = fields_offset + len(FIELDS) * n * d * 8
    if path.stat().st_size != expected:
        raise RuntimeError(f"{path}: wrote {path.stat().st_size} bytes, expected {expected}")


def _nan_runs(missing: np.ndarray) -> list[tuple[int, int]]:
    """(start, end) inclusive index pairs of contiguous True runs."""
    idx = np.flatnonzero(missing)
    if idx.size == 0:
        return []
    br = np.flatnonzero(np.diff(idx) > 1)
    return list(zip(np.r_[idx[0], idx[br + 1]], np.r_[idx[br], idx[-1]]))


def build_panel(end: date, years: float) -> dict[str, np.ndarray]:
    cal = trading_calendar(end - timedelta(days=round(years * 365.25)), end)
    start = cal[0].date()

    etfs = sorted(SECTOR_PEERS)
    universe = sorted(t for t in load_universe() if t != SPY and t not in etfs)
    order = [SPY, *etfs, *universe]
    kinds = {SPY: KIND_BENCHMARK, **{e: KIND_SECTOR_ETF for e in etfs}}

    logger.info("Universe %d + SPY + %d ETFs, %d sessions %s .. %s",
                len(universe), len(etfs), len(cal), start, end)
    data, source = fetch_history(order, start, end)

    missing = [t for t in order if t not in data]
    for t in missing:
        if t in kinds:
            raise RuntimeError(f"no history for required symbol {t}")
    symbols = [t for t in order if t in data]

    n, d = len(symbols), len(cal)
    arrays = {f: np.full((n, d), np.nan) for f in FIELDS}
    first = np.zeros(n, dtype=np.int32)
    off_calendar = 0
    trailing: dict[str, str] = {}             # symbol -> last bar date
    reused: dict[str, tuple[str, str]] = {}   # symbol -> (old listing last bar, new listing first bar)
    holes: dict[str, int] = {}                # symbol -> NaN sessions

    for i, t in enumerate(symbols):
        df = data[t]
        on_cal = df.index.isin(cal)
        off_calendar += int((~on_cal).sum())
        aligned = df.loc[on_cal, FIELDS].reindex(cal)
        missing_bar = aligned["Close"].isna().to_numpy()
        lo = int(np.argmax(~missing_bar))

        # Pass 1: cut at the last long interior gap (reused symbol)
        for a, b in _nan_runs(missing_bar[lo:]):
            a, b = a + lo, b + lo
            if b < d - 1 and b - a + 1 >= REUSED_SYMBOL_GAP:
                reused[t] = (str(cal[a - 1].date()), str(cal[b + 1].date()))
        if t in reused:
            lo = cal.get_loc(pd.Timestamp(reused[t][1]))
            aligned.iloc[:lo] = np.nan

        # Pass 2: classify what is left
        for a, b in _nan_runs(missing_bar[lo:]):
            a, b = a + lo, b + lo
            if b == d - 1:
                trailing[t] = str(cal[a - 1].date())
            else:
                holes[t] = holes.get(t, 0) + (b - a + 1)

        first[i] = lo
        for f in FIELDS:
            arrays[f][i] = aligned[f].to_numpy()

    src = [source[t] for t in symbols]
    logger.info(
        "Panel %d x %d  sources: alpaca=%d yahoo=%d yahoo_longer=%d  "
        "no data=%d  left-padded=%d  off-calendar bars dropped=%d",
        n, d, src.count("alpaca"), src.count("yahoo"), src.count("yahoo_longer"),
        len(missing), int((first > 0).sum()), off_calendar,
    )
    if missing:
        logger.info("No data from either source (%d): %s", len(missing), " ".join(missing[:40]))
    if trailing:
        logger.warning("Stopped trading before %s (%d): %s", end, len(trailing),
                       " ".join(f"{t}@{dt}" for t, dt in sorted(trailing.items(), key=lambda kv: kv[1])))
    if reused:
        logger.warning("Reused symbols, older listing blanked (%d): %s", len(reused),
                       " ".join(f"{t}[{a}|{b}]" for t, (a, b) in sorted(reused.items())))
    if holes:
        logger.info("Short interior holes left NaN: %d symbols, %d sessions",
                    len(holes), sum(holes.values()))

    return {
        "dates":  cal.strftime("%Y%m%d").astype(np.int32).to_numpy(),
        "symbols": np.array(symbols),
        "kind":   np.array([kinds.get(t, KIND_EQUITY) for t in symbols], dtype=np.int8),
        "source": np.array(src),
        "first":  first,
        **{f.lower(): arrays[f] for f in FIELDS},
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    ap.add_argument("--end", type=date.fromisoformat, default=None,
                    help="last session to include (default: last completed NYSE session)")
    ap.add_argument("--years", type=float, default=4.0)
    ap.add_argument("--panel", type=Path, default=Path("engine/data/history_panel.npz"))
    ap.add_argument("--from-panel", action="store_true",
                    help="load --panel instead of fetching")
    ap.add_argument("--bin", type=Path, default=Path("engine/data/history.bin"))
    args = ap.parse_args()

    if args.from_panel:
        panel = dict(np.load(args.panel))
        logger.info("Loaded %s: %d symbols x %d sessions",
                    args.panel, len(panel["symbols"]), len(panel["dates"]))
    else:
        panel = build_panel(args.end or last_completed_session(), args.years)
        args.panel.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(args.panel, **panel)
        logger.info("Wrote %s (%.1f MB)", args.panel, args.panel.stat().st_size / 1e6)

    args.bin.parent.mkdir(parents=True, exist_ok=True)
    write_history_bin(panel, args.bin)
    logger.info("Wrote %s (%.1f MB, %d symbols x %d sessions)", args.bin,
                args.bin.stat().st_size / 1e6, len(panel["symbols"]), len(panel["dates"]))


if __name__ == "__main__":
    main()
