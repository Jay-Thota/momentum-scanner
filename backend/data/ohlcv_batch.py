"""
backend/data/ohlcv_batch.py

Daily OHLCV for the whole universe as (T, D) arrays, built straight from the
raw Alpaca batch response (JSON dicts, no alpaca-py Bar models). The C++ engine reads the arrays as they are; nothing
becomes a pandas DataFrame unless some later step asks for that ticker.

Layout matches what mscan_engine.score_l1l2_batch expects: row i holds ticker
i's bars in order, right-aligned and left-padded with NaN to the longest
history D. `dates` holds each bar's YYYYMMDD (0 for padding), the same
calendar date layer2 prints with str(index)[:10].

OhlcvBatch.frame(t) rebuilds exactly the DataFrame fetcher_price.bars_to_frame
would have built (same values, index dtype, timezone and OBV), so the Python
layers and everything after Step 5 see no difference.

LazyOhlcvMap wraps a batch as the {ticker: DataFrame} mapping run_daily_scan
passes around, building and caching each DataFrame on first access.
"""

import warnings
from collections.abc import MutableMapping
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from pydantic_core import TzInfo

from backend import config
from backend.data.fetcher_price import _compute_obv

FIELDS = ("Open", "High", "Low", "Close", "Volume")


@dataclass
class OhlcvBatch:
    tickers: list[str]
    lengths: np.ndarray                  # (T,) bars per ticker
    arrays: dict[str, np.ndarray]        # field -> (T, D) float64, NaN-padded
    dates: np.ndarray                    # (T, D) int64 YYYYMMDD, 0 for padding
    # Alpaca rows only: bar timestamps (UTC microseconds) and their tzinfo, so
    # frame() can rebuild the index exactly. None for rows built from frames.
    ts_us: np.ndarray | None = None
    tz: object = None
    row: dict[str, int] = field(init=False)

    def __post_init__(self) -> None:
        self.row = {t: i for i, t in enumerate(self.tickers)}

    @property
    def width(self) -> int:
        return self.arrays["Close"].shape[1]

    # ---- construction -----------------------------------------------------

    @classmethod
    def from_raw_bars(cls, raw_by_ticker: dict[str, list[dict]], tickers: list[str]) -> "OhlcvBatch":
        """
        Alpaca's raw bars response ({ticker: [{"t", "o", "h", "l", "c", "v", ...}]},
        from a client with raw_data=True) -> batch, one row per ticker in
        `tickers` (empty rows for tickers with no data). Skips building
        alpaca-py Bar models, which costs more than everything else here.
        Volume is scaled by ALPACA_IEX_VOLUME_MULTIPLIER as bars_to_frame does.
        """
        rows = [raw_by_ticker.get(t) or [] for t in tickers]
        lengths = np.array([len(r) for r in rows], dtype=np.int64)
        d = int(lengths.max()) if len(lengths) else 0
        arrays = {f: np.full((len(tickers), d), np.nan) for f in FIELDS}
        ts_us = np.zeros((len(tickers), d), dtype=np.int64)

        # Every timestamp ("2026-10-28T04:00:00Z") parsed in one vectorized call.
        # numpy parses UTC "Z" strings ~3x faster than pandas once the Z is
        # stripped (its datetime64 is UTC-naive); anything else goes to pandas.
        stamps = [bar["t"] for r in rows for bar in r]
        if stamps and all(s[-1] == "Z" for s in stamps):
            all_us = np.array([s[:-1] for s in stamps], dtype="datetime64[us]").astype(np.int64)
        elif stamps:
            all_us = pd.to_datetime(stamps, utc=True, format="ISO8601").as_unit("us").asi8
        offset = 0
        for i, r in enumerate(rows):
            n = len(r)
            if n == 0:
                continue
            vals = np.array([(b["o"], b["h"], b["l"], b["c"], b["v"]) for b in r], dtype=np.float64)
            us = all_us[offset:offset + n]
            offset += n
            order = np.argsort(us, kind="stable")   # bars_to_frame sorts by time
            if not np.array_equal(order, np.arange(n)):
                vals, us = vals[order], us[order]
            for j, f in enumerate(FIELDS):
                arrays[f][i, d - n:] = vals[:, j]
            arrays["Volume"][i, d - n:] *= config.ALPACA_IEX_VOLUME_MULTIPLIER
            ts_us[i, d - n:] = us

        dates = np.zeros((len(tickers), d), dtype=np.int64)
        mask = lengths[:, None] > np.arange(d)[::-1][None, :]   # real bars, not padding
        if mask.any():
            idx = pd.DatetimeIndex(ts_us[mask].astype("datetime64[us]"))   # UTC calendar date
            dates[mask] = idx.year * 10000 + idx.month * 100 + idx.day
        # alpaca-py's Bar models carry pydantic's UTC tzinfo; reuse it so frame()
        # rebuilds the identical index (datetime64[us, UTC] with TzInfo(0))
        return cls(list(tickers), lengths, arrays, dates, ts_us, TzInfo(0))

    @classmethod
    def from_frames(cls, tickers: list[str], frames: dict[str, pd.DataFrame]) -> "OhlcvBatch":
        """Batch from per-ticker DataFrames (Yahoo fallback rows, tests)."""
        dfs = [frames.get(t, pd.DataFrame()) for t in tickers]
        lengths = np.array([len(df) for df in dfs], dtype=np.int64)
        d = int(lengths.max()) if len(lengths) else 0
        arrays = {f: np.full((len(tickers), d), np.nan) for f in FIELDS}
        dates = np.zeros((len(tickers), d), dtype=np.int64)
        for i, df in enumerate(dfs):
            n = len(df)
            if n == 0:
                continue
            for f in FIELDS:
                arrays[f][i, d - n:] = df[f].to_numpy(dtype=np.float64)
            idx = pd.DatetimeIndex(df.index)   # date in the index's own timezone, as str()[:10]
            dates[i, d - n:] = idx.year * 10000 + idx.month * 100 + idx.day
        return cls(list(tickers), lengths, arrays, dates)

    def combine(self, other: "OhlcvBatch") -> "OhlcvBatch":
        """This batch with `other`'s rows appended, replacing any ticker in both."""
        keep = np.array([t not in other.row for t in self.tickers], dtype=bool)
        tickers = [t for t, k in zip(self.tickers, keep) if k] + list(other.tickers)
        lengths = np.concatenate([self.lengths[keep], other.lengths])
        d = max(self.width, other.width)
        nk = int(keep.sum())

        # Rows are right-aligned, so each source is one block copy into the
        # last `width` columns of the wider result.
        def stack(a: np.ndarray | None, b: np.ndarray | None, fill, dtype) -> np.ndarray:
            out = np.full((len(tickers), d), fill, dtype=dtype)
            if a is not None and self.width:
                out[:nk, d - self.width:] = a[keep]
            if b is not None and other.width:
                out[nk:, d - other.width:] = b
            return out

        arrays = {f: stack(self.arrays[f], other.arrays[f], np.nan, np.float64) for f in FIELDS}
        dates = stack(self.dates, other.dates, 0, np.int64)
        ts_us = stack(self.ts_us, other.ts_us, 0, np.int64)
        return OhlcvBatch(tickers, lengths, arrays, dates, ts_us, self.tz or other.tz)

    # ---- access -----------------------------------------------------------

    def frame(self, ticker: str) -> pd.DataFrame:
        """The DataFrame bars_to_frame would have built for this ticker."""
        i = self.row[ticker]
        n = int(self.lengths[i])
        if n == 0:
            return pd.DataFrame()
        sl = slice(self.width - n, None)
        if self.ts_us is None or self.ts_us[i, sl][0] == 0:   # no bar is stamped 1970-01-01
            raise ValueError(f"{ticker}: row was built from a DataFrame; keep that frame instead")
        df = pd.DataFrame({f: self.arrays[f][i, sl].copy() for f in FIELDS})
        df.index = pd.DatetimeIndex(self.ts_us[i, sl].astype("datetime64[us]")).tz_localize(self.tz)
        df["OBV"] = _compute_obv(df)
        return df

    def rows(self, tickers: list[str]) -> tuple[dict[str, np.ndarray], np.ndarray]:
        """(T', D) arrays and dates for these tickers, in this order."""
        idx = np.array([self.row[t] for t in tickers], dtype=np.int64)
        return {f: self.arrays[f][idx] for f in FIELDS}, self.dates[idx]

    def last_close_and_avg_volume(self, tickers: list[str], window: int) -> tuple[np.ndarray, np.ndarray]:
        """
        Close on the last bar and mean volume over the last `window` bars, for
        rows with at least `window` bars (NaN otherwise). Same values as
        df["Close"].iloc[-1] and df["Volume"].tail(window).mean(): pandas'
        mean skips NaN like nanmean, and whole-share volumes sum exactly in
        any order.
        """
        arrays, _ = self.rows(tickers)
        lengths = np.array([self.lengths[self.row[t]] for t in tickers], dtype=np.int64)
        ok = lengths >= window
        close = np.full(len(tickers), np.nan)
        avg = np.full(len(tickers), np.nan)
        close[ok] = arrays["Close"][ok, -1]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)   # all-NaN windows -> NaN, as pandas
            avg[ok] = np.nanmean(arrays["Volume"][ok, -window:], axis=1)
        return close, avg


class LazyOhlcvMap(MutableMapping):
    """
    {ticker: DataFrame} view of an OhlcvBatch. DataFrames are built on first
    access and cached. Assigned or update()d frames (e.g. the Yahoo fallback)
    are kept as given and also merged into the batch so the engine sees them.
    """

    def __init__(self, batch: OhlcvBatch) -> None:
        self.batch = batch
        self._frames: dict[str, pd.DataFrame] = {}

    def __getitem__(self, ticker: str) -> pd.DataFrame:
        if ticker not in self._frames:
            if ticker not in self.batch.row:
                raise KeyError(ticker)
            self._frames[ticker] = self.batch.frame(ticker)
        return self._frames[ticker]

    def __setitem__(self, ticker: str, df: pd.DataFrame) -> None:
        self.update({ticker: df})

    def update(self, other=(), /, **kwargs) -> None:
        frames = dict(other, **kwargs)
        if frames:
            self._frames.update(frames)
            self.batch = self.batch.combine(OhlcvBatch.from_frames(list(frames), frames))

    def __delitem__(self, ticker: str) -> None:
        raise TypeError("LazyOhlcvMap does not support deletion")

    def __iter__(self):
        return iter(self.batch.tickers)

    def __len__(self) -> int:
        return len(self.batch.tickers)

    def n_rows(self, ticker: str) -> int:
        """Bars for a ticker without building its DataFrame (0 if unknown)."""
        i = self.batch.row.get(ticker)
        return 0 if i is None else int(self.batch.lengths[i])
