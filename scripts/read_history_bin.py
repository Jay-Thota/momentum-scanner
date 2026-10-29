"""
python -m scripts.read_history_bin [--bin engine/data/history.bin] [--panel engine/data/history_panel.npz]

Reads the engine's MSCANV01 history file and round-trips it against the .npz
panel it was written from.  Parsing is written independently of the writer in
export_history.py (no shared constants), so a layout mistake on either side
shows up as a failure here rather than cancelling out.

read_history_bin() maps the file with np.memmap, the same way
Universe::open_mmap will, and returns zero-copy (num_tickers, num_days) views.
"""

import argparse
import sys
from pathlib import Path

import numpy as np

SEP = "=" * 68

# Field-by-field layout of mscan::FileHeader (little-endian, 40 bytes)
_HEADER_DTYPE = np.dtype([
    ("magic",          "S8"),
    ("version",        "<u4"),
    ("num_tickers",    "<u4"),
    ("num_days",       "<u4"),
    ("_pad",           "V4"),
    ("tickers_offset", "<u8"),
    ("fields_offset",  "<u8"),
])
_FIELDS = ("open", "high", "low", "close", "volume")


def read_history_bin(path: Path) -> dict:
    raw = np.memmap(path, dtype=np.uint8, mode="r")
    if raw.size < _HEADER_DTYPE.itemsize:
        raise ValueError(f"{path}: {raw.size} bytes is smaller than the header")
    hdr = raw[:_HEADER_DTYPE.itemsize].view(_HEADER_DTYPE)[0]
    if bytes(hdr["magic"]) != b"MSCANV01":
        raise ValueError(f"{path}: bad magic {bytes(hdr['magic'])!r}")

    n, d = int(hdr["num_tickers"]), int(hdr["num_days"])
    t_off, f_off = int(hdr["tickers_offset"]), int(hdr["fields_offset"])
    expected = f_off + len(_FIELDS) * n * d * 8
    if raw.size != expected:
        raise ValueError(f"{path}: size {raw.size}, header implies {expected}")

    dates = raw[_HEADER_DTYPE.itemsize:_HEADER_DTYPE.itemsize + 4 * d].view("<i4")
    names = bytes(raw[t_off:f_off]).rstrip(b"\0").split(b"\0")
    fields = raw[f_off:].view("<f8").reshape(len(_FIELDS), n, d)

    return {
        "header": {k: hdr[k] for k in _HEADER_DTYPE.names if not k.startswith("_")},
        "pad": bytes(hdr["_pad"]),
        "tickers_end": t_off + sum(len(s) + 1 for s in names),
        "dates": dates,
        "symbols": [s.decode("ascii") for s in names],
        **{f: fields[i] for i, f in enumerate(_FIELDS)},
    }


def _first_valid(close: np.ndarray) -> np.ndarray:
    """First non-NaN close per ticker, as Universe precomputes at load."""
    valid = ~np.isnan(close)
    return np.where(valid.any(axis=1), valid.argmax(axis=1), close.shape[1]).astype(np.int32)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    ap.add_argument("--bin", type=Path, default=Path("engine/data/history.bin"))
    ap.add_argument("--panel", type=Path, default=Path("engine/data/history_panel.npz"))
    args = ap.parse_args()

    b = read_history_bin(args.bin)
    p = np.load(args.panel)
    h = b["header"]
    print(f"\n{SEP}\n  MSCANV01 round trip: {args.bin} vs {args.panel}\n{SEP}")
    print(f"  version={h['version']}  num_tickers={h['num_tickers']}  num_days={h['num_days']}"
          f"  tickers_offset={h['tickers_offset']}  fields_offset={h['fields_offset']}")

    checks = [
        ("version is 1", h["version"] == 1),
        ("pad bytes are zero", b["pad"] == b"\0" * 4),
        ("dates follow the 40-byte header", h["tickers_offset"] == 40 + 4 * h["num_days"]),
        ("fields_offset is 8-byte aligned", h["fields_offset"] % 8 == 0),
        ("ticker blob ends before fields_offset", b["tickers_end"] <= h["fields_offset"]),
        ("dates identical", np.array_equal(b["dates"], p["dates"])),
        ("symbols identical, same order", b["symbols"] == [str(s) for s in p["symbols"]]),
    ]
    for f in _FIELDS:
        # Compare raw bits, so NaN == NaN and -0.0 != 0.0
        same = np.array_equal(b[f].view(np.uint64), np.ascontiguousarray(p[f]).view(np.uint64))
        checks.append((f"{f} bit-identical ({b[f].shape[0]} x {b[f].shape[1]})", same))
    checks.append(("first-valid day matches panel", np.array_equal(_first_valid(b["close"]), p["first"])))

    ok = True
    for label, cond in checks:
        print(f"  {'[PASS]' if cond else '[FAIL]'}  {label}")
        ok &= bool(cond)

    spy = b["symbols"].index("SPY")
    last = int(h["num_days"]) - 1
    print(f"\n  Sample: SPY {b['dates'][last]}  "
          + "  ".join(f"{f}={b[f][spy, last]:.6g}" for f in _FIELDS))
    print(f"\n{SEP}\n  {'ALL PASSED' if ok else 'FAILURES ABOVE'}\n{SEP}\n")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
