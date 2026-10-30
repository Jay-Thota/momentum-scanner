# Engine design notes

Plain-language notes on the key decisions in each phase, written to study
before interviews. Each answer is short enough to say out loud.

---

## Phase 1: history export and golden files

**Why export to a custom binary file instead of CSV or Parquet?**
The backtester reads the whole universe (3,289 tickers x 1,003 days) on every
run. A flat binary file can be memory-mapped and used in place: no parsing,
no copying, and the OS only loads the pages that are actually touched.

**What does the file look like?**
A 40-byte header (magic `MSCANV01`, version, counts, two offsets), the dates
as int32 `YYYYMMDD`, the ticker names as NUL-terminated strings, then five
arrays of doubles (open, high, low, close, volume). Each array is laid out
`[ticker * num_days + day]`, so one ticker's history is contiguous in memory.

**Why is the header 40 bytes and not 36?**
The C++ compiler puts 4 bytes of padding before the first `uint64_t` so it
lands on an 8-byte boundary. The Python writer writes those 4 bytes
explicitly (`struct` format `<8sIII4xQQ`). We confirmed `sizeof == 40` by
compiling a test program against the header.

**Why pad the start of the price arrays to a multiple of 8?**
So every double sits at an 8-byte-aligned address once the file is mapped
(`mmap` returns a page-aligned base). Misaligned loads are slow on some CPUs
and undefined behavior in C++.

**Why one shared trading calendar with NaN padding?**
Every ticker has the same number of days, so "who qualifies on day d" is a
walk down one column, and backtest dates never need re-aligning. A ticker
that listed late gets NaN for the days before its first bar. Missing days
are never forward-filled, because a filled price is a made-up price.

**What did the raw data get wrong, and what did we do about it?**
- Alpaca sometimes starts a long-listed stock's history recently (TEVA from
  2026-09). We check those against yfinance and use the longer series whole,
  never spliced, because the two sources scale volume differently.
- Reused ticker symbols: BBBY's history was two different companies with a
  two-year gap. We blank everything before the gap.
- Stale universe: 67 tickers stopped trading after the universe was cached.
  They are logged; their later days stay NaN.

**How do you prove a C++ port matches the Python?**
Golden files. A script runs the real Python code on 68 cases and writes the
inputs and every output. Floats are written with Python `repr`, the shortest
string that reads back to the exact same double, so C++ can demand bit-for-bit
equality instead of "close enough".

**Why synthetic test series?**
Real data almost never lands exactly on a threshold. We built series where
the breakout is exactly +8.0% on exactly 2.0x volume, one floating-point step
below that, and ADX exactly 20.0. Those test whether `>=` versus `>` was
ported correctly, which real data can't.

**What's the look-ahead trap in golden files?**
For a case cut at a past date, the sector ETF must be cut at the same date.
Otherwise layer 2 would see the ETF's future prices.

---

## Phase 2: memory-mapped universe and indicator kernels

**How does `Universe::open_mmap` avoid leaking on errors?**
RAII. The file descriptor lives in a small struct that closes it in its
destructor. The moment `mmap` succeeds, the mapping is handed to the
`Universe` object, so if any validation check throws, the destructor unmaps
it during stack unwinding. There's no cleanup code to forget.

**Why can't a `Universe` be copied, only moved?**
It owns the mapping. Two copies would both call `munmap` on the same memory.
Moving uses `std::exchange` to take the pointer and leave the source null, so
exactly one object ever unmaps.

**Why `memcpy` the header instead of casting the bytes to `FileHeader*`?**
Casting raw bytes to a struct pointer runs into C++'s alignment and
strict-aliasing rules. Copying 40 bytes costs nothing and is always legal.

**What does the loader check before trusting the file?**
Magic, version, that the dates follow the header, offset order, 8-byte
alignment, that `num_tickers * num_days * 40` can't overflow, and that the
file is exactly the size the header implies. A truncated file fails loudly
instead of reading past the end.

**How does the design prevent look-ahead bias?**
`series(i, d)` returns `std::span` views that end at day d. Scoring code gets
nothing else, so it physically can't read day d+1.

**Why does the SMA use Kahan summation instead of a running sum?**
To match pandas exactly. A plain running sum (add the new value, subtract the
old) accumulates rounding error: after 1e15 passes through a window of small
numbers, the mean stays wrong by about 0.008 forever. Kahan summation carries
the rounding error from each step in a second variable and feeds it back in.
pandas does this, so we do the same, operation for operation.

**What else does the pandas rolling mean do?**
- Separate error terms for adding and removing values.
- A repeated-value shortcut: if every value in the window is identical, the
  mean is that value exactly. Otherwise a flat stock could show
  `close > ma20` from rounding noise.
- Sign clamping: if every input is >= 0, a slightly negative result from
  rounding is clamped to 0, and the reverse for all-negative inputs.

**Why `-ffp-contract=off`?**
Compilers may fuse `a*b + c` into one fused multiply-add instruction. It's
more accurate (one rounding instead of two), but numpy doesn't do it, so
results would differ by a few ULPs and bit-exact parity would fail. We also
never use `-ffast-math`, which would let the compiler optimize Kahan
summation away entirely.

**How do you prove a kernel doesn't allocate?**
The test binary replaces the global `operator new` with a version that counts
calls. Each test reads the counter, calls the kernel, and checks the count
didn't change. A separate test proves the counter really counts.

**How does ADX avoid scratch buffers?**
ADX chains four rolling means (true range, +DM, -DM, then DX). A rolling mean
only needs the value leaving the window, and each of those can be recomputed
for bar `i - period`: true range and DM from the input bars, DX from the
+DI/-DI already written to the output. One pass, a few scalars of state, no
allocation.

**Why is the ADX "wrong"?**
Textbook ADX uses Wilder's smoothing; the Python reference uses a plain
rolling mean. The C++ port matches the Python first. Switching to Wilder would
be a deliberate, separately tested change.

**What do the sanitizers catch?**
AddressSanitizer catches out-of-bounds reads and use-after-free (e.g. reading
a span after its `Universe` is destroyed). UndefinedBehaviorSanitizer catches
things like signed overflow and misaligned loads. The full suite runs clean
under both. LeakSanitizer isn't supported on Apple Silicon, which is one reason
leak-safety here comes from RAII rather than from tooling.

---

## Phase 3: layer scoring in C++, Python bindings, Step 4 switch

**Why only layers 1 and 2 move to C++?**
They only need price and volume history, which is pure number crunching on
arrays. Layer 0's market cap and earnings dates, layer 3's options chains and
layer 4's short interest come from network APIs; C++ wouldn't make the
network faster.

**How do you prove a port of business logic is exact, not just close?**
Three layers of evidence. Golden files check every reported field for 68
hand-picked cases (every archetype, short histories, exact-threshold series).
A Python test runs the real scan function both ways on the whole 3,289-ticker
universe and requires identical dicts. Edge-case series (NaN volume on a
breakout day, NaN closes, zero volume, missing days, UTC timestamps) cover
what real data rarely shows.

**What was the surprising part of matching the Python?**
Python has two `round()` functions. `round(x, 2)` on a Python float rounds the
exact binary value to the nearest decimal; on a `numpy.float64` it computes
`rint(x * 100) / 100`. They disagree on about 2% of inputs (2.675 -> 2.67 vs
2.68). Which one runs depends on whether a value happened to come out of
pandas, so each output field had to be traced to its type.

**How does NaN behave in the port?**
Identically, because comparisons with NaN are false in both languages. That
includes oddities: a NaN close is not rejected by the uptrend check (both
`<=` tests are false), and a NaN volume does not skip a breakout candidate.
The port copies these rather than "fixing" them, so the two paths can be
compared bit for bit; behavior fixes are separate changes.

**Does summation order matter?**
In general yes: numpy sums with a pairwise algorithm, a simple loop sums left
to right, and the rounding differs. Here the only things summed are share
volumes, which are whole numbers far below 2^53, so every partial sum is exact
and the order can't change the result. The code says so where it relies on it.

**How do Python and C++ share memory without copying?**
pybind11 receives numpy arrays as `py::array_t<double>`, requiring C order
(converting only if needed). `Universe::from_buffers` wraps the raw pointers;
nothing is copied. The pointers are taken while holding the GIL, then the
GIL is released for the scoring loop, which never touches a Python object.
The arrays stay alive because they're arguments of the call.

**Why are the rows "right-aligned" instead of on one calendar?**
In the daily scan each ticker's row is its own bars in order, padded with
NaN in front. A ticker missing a day therefore gets scored on exactly the bars
the Python code sees. A shared calendar would add a NaN row for the missing
day and change the rolling windows. Dates travel in a parallel array so a
breakout still reports the right date.

**How do you ship a rewrite safely?**
Behind a flag (`USE_CPP_ENGINE`, off by default), with the old path kept.
If the flag is on but the module is missing or throws, the scan logs a
warning and uses Python. Even the JSON stored in the database is identical:
one Python signal is a numpy bool that gets stored as the string "True", so
the C++ path returns the same type.

**What did the benchmark show?**
On 3,289 tickers (Apple M3 Pro, one thread): Python 7.45 s, C++ path 474 ms,
so 15.7x end to end. But the engine call is only 28 ms (268x); 445 ms is
Python reshaping DataFrames into arrays. That's Amdahl's law: speeding up one
part only helps as much as that part's share of the total. The next win is
building arrays directly from the API response instead of via DataFrames,
not a faster engine or more threads.

**How is the module built?**
`pip install -e engine` uses scikit-build-core to run CMake and install the
compiled module into the venv. Tests are switched off in that build so pip
never downloads googletest. Plain CMake builds still produce the tests and
the module for development.
