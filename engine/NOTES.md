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

---

## Phase 4: data path, thread pool, benchmarks

**Phase 3 said 15.7x faster. Why did phase 4 start by measuring again?**
Phase 3 timed only the step it changed. Timing from the API response to the
end of Step 4 showed where the time really went: 3.1 s for alpaca-py to turn
the JSON into Bar objects and 3.0 s to turn those into DataFrames, against
36 ms in the engine. End to end, phase 3 was only 2.2x faster. Lesson: time
the whole path a user waits on, not the part you just rewrote.

**What changed in the data path?**
With the C++ path on, the scan asks Alpaca for raw JSON and fills the engine's
arrays straight from it, parsing all timestamps in one vectorized call. No Bar
objects and no DataFrames for the ~3,300 tickers. A lazy mapping builds a
DataFrame only when a later step asks for that ticker (about 1,500 survivors).
Response to end of Step 4: 10.5 s to 0.28 s (37x); counting the 0.63 s of
later survivor DataFrames, 0.91 s (11.5x). (On AC power, Release build; the
first measurement, on battery in Low Power Mode, was 15.7 s to 0.41 s.)

**How do you keep the downstream code working?**
`LazyOhlcvMap` behaves like the `{ticker: DataFrame}` dict the scan always
passed around, so later steps didn't change. The DataFrames it builds are
identical to the old ones, down to the index's timezone object, and a test
checks that for every ticker in the real universe.

**How does the thread pool work?**
A queue of tasks guarded by one mutex, and N worker threads that sleep on a
condition variable until there is work. `submit` wraps the task in a
`std::packaged_task`, so its return value or exception lands in a
`std::future` the caller can wait on.

**Why one shared queue and not work stealing?**
Tasks here are coarse: one chunk of ~100 tickers taking milliseconds. Taking
a lock once per chunk is noise. Work stealing (a queue per thread, idle
threads stealing from others) pays off when tasks are tiny and numerous and
the shared lock becomes the bottleneck.

**What happens on shutdown?**
The destructor marks the pool as stopping (new submits throw), asks every
thread to stop, and joins them. Workers keep taking tasks until the queue is
empty, so queued work still runs. The join must happen in the destructor
body: C++ destroys members in reverse order of declaration, so if the threads
were joined by the vector's own destructor, the mutex and condition variable
would already be gone while threads still used them.

**Why `std::jthread` and `condition_variable_any`?**
A jthread carries a stop token and joins itself when destroyed.
`condition_variable_any::wait` can take that stop token, so a sleeping
worker wakes up when stop is requested; no extra flag or broadcast needed.

**What if a task throws?**
packaged_task catches it and stores it in the future, so the worker thread
survives and the exception reappears where the caller calls `.get()`.
`parallel_for` waits for every chunk before rethrowing, because the chunks
hold a reference to the caller's function.

**Why ~4 chunks per thread in parallel_for?**
Tickers cost different amounts (a 4-year history costs much more than a
recent IPO). With exactly one chunk per thread, everyone waits for the
slowest chunk. With several smaller chunks, threads that finish early pick
up more, so they all finish at about the same time.

**How do you test concurrent code?**
Tests for each promise the pool makes (every index covered exactly once,
queued work finishes on shutdown, submit during shutdown throws, exceptions
reach the caller), run many times, then the whole suite under
ThreadSanitizer, which flags unsynchronized access even when the test
happened to pass.

**Where does scaling stop, and why?** (M3 Pro: 5 performance + 6 efficiency
cores; AC power, Low Power Mode off, Release build, phase 4 code)

| threads | backtest, every day (3.07M scores) | production scan |
|---|---|---|
| 1 | 34.9 s | 23.2 ms |
| 2 | 18.0 s (1.9x) | 12.0 ms (1.9x) |
| 4 | 9.49 s (3.7x) | 6.56 ms (3.5x) |
| 8 | 6.64 s (5.3x) | 6.91 ms (3.4x) |
| 11 | 6.28 s (5.6x) | 7.66 ms (3.0x) |

The backtest is close to linear up to 4 threads: each ticker's data and
scratch arrays fit in the core's own caches, and threads share nothing but
one atomic add per chunk. Past 5 threads the extra threads land on the
slower efficiency cores, so each one adds less.

The production scan stops at 4 threads and gets slower after. At the time I
put that down to fork/join overhead on a 23 ms job. That was wrong, or at
least not the main reason: phase 5 found that `py_round` used `snprintf` and
`strtod`, which on macOS touch shared global locale state, so every thread
contended on it. Scoring a day from scratch is expensive enough to hide that
in the backtest; the short production job is not. With the rounding fixed
the same scan scales to 6.5x at 11 threads (see phase 5). Lesson: when
scaling stops, profile before explaining it.

**What's still slow?**
The backtest recomputes every indicator over the whole history for every
day, so its cost grows with days squared: 35 s single-threaded. Phase 5's
streaming indicators update each day in O(1) instead. That is a far bigger
win than any number of threads.

**The "True" string bug.**
Layer 1 signal C came out of a numpy comparison, so it was a numpy bool, and
`json.dumps(..., default=str)` stored it as the string "True" or "False".
In JavaScript the string "False" is truthy. Phase 3 matched the quirk so the
two paths could be compared exactly; phase 4 fixed it in both paths in its
own commit. Rows stored before the fix still hold the strings; the API
converts them back to booleans when it reads them (no migration).

## Phase 5: streaming indicators, backtester, CI, deployment

**Why was the phase 4 backtest quadratic?**
Scoring day d recomputed every indicator over the whole history up to d. A
ticker with D days costs 1 + 2 + ... + D bar updates, which grows with D
squared. 3.07M (ticker, day) scores took 34 s single-threaded.

**What are streaming indicators?**
`StreamingIndicators` holds the state each indicator needs and moves it
forward by one bar in O(1): the pandas rolling-mean state for each SMA and
each ADX term, the running OBV total, and short rings of recent values (the
close leaving the 200-day window, the ADX terms leaving the 14-day one, the
50/200-day SMAs from 5 and 10 days ago). Each day costs the same no matter
how much history came before, so the backtest is linear in days. Same
3.07M scores: 34.4 s to 0.62 s (55x).

**How do you know streaming gives the same numbers?**
It feeds the same values into the same `PandasRollingMean` in the same
order as the batch kernels, starting from the same first bar, so the
floating-point operations are identical, not just close. The rolling mean and
ADX formulas moved into `detail/` headers so both paths share one copy.
Tests stream synthetic series (flat runs, NaN holes), all 68 goldens and
every ticker in the 4-year file, and compare every value at every bar
bit for bit.

**Why do the scores also match without duplicating the layer code?**
The layers only read raw bars near the newest day; everything that grows
with history is an indicator series. So `score_layer1/2` take an
`IndicatorSnapshot` (the newest indicator values plus the lagged SMAs) and
the batch versions build that same snapshot from their full vectors. One
scoring code path, two ways to fill the snapshot. A test scores every bar
of every golden both ways and requires every result field to match.

**How does the rolling max work?**
OBV must be compared with its 20-day high every day. A monotonic deque keeps
candidates in decreasing order: a new value pops every smaller value off the
back (they can never be the max again while it is in the window), and the
front drops out by index once it leaves the window. Each value enters and
leaves once, so it's O(1) amortized. It's tested against brute force with
NaNs and many equal values.

**Where does the memory live?**
In fixed-size `std::array`s inside the object, about 6 KB, so `update()`
never touches the heap (a test counts allocations) and each worker keeps
its ticker's state on its own stack.

**How is look-ahead ruled out?**
By structure and by test. Selection only sees `series(i, d)` and streaming
state fed bars up to d. Forward returns are computed in a separate pass
after all picks are made. The test replaces every bar after day d with
garbage (huge values, zeros, negatives, NaN, SPY included) and requires the
picks through d to be unchanged. I proved the test can fail by planting a
one-day leak in the price gate: it failed.

**How does the backtest use threads?**
Workers split the tickers; each streams its tickers through every day and
writes scores into its own slice of a ticker-major matrix, so no two
workers write the same memory and no locks are needed. Picking the top N
per day then runs on one thread. Results are identical for any thread
count (tested).

**What does it reproduce from the live scan?**
Step 2's price/volume gate, layers 1 and 2 with Step 4's inputs, and the
regime rule from `regime_filter` (on SPY, since that's what the file has).
In a bear regime the live scan raises the bar from 4.0 to 5.0, so the
backtest raises its PV threshold too. `validate_backtest` re-derives sampled
days' picks with the scan's own Python functions; they match exactly.

**How are Sharpe and drawdown computed?**
Each day's picks get 1/h of the capital for h days (a staggered portfolio),
which gives one real return per day instead of overlapping h-day returns.
Sharpe is mean / stdev * sqrt(252) with no risk-free rate; drawdown is the
largest fall from a peak of that portfolio.

**Why did 8 threads run slower than 1?**
Profiling showed rounding was 80% of streaming time: `py_round` formatted
with `snprintf` and parsed with `strtod`, and on macOS both touch shared
global locale state, so threads fought over it. Two fixes, each its own
commit. First `std::to_chars`/`from_chars`, which the standard defines as
printf/strtod in the "C" locale but which touch no shared state. Then an
exact shortcut: Python rounds the exact value of x * 10^n, and
`fma(x, p, -x*p)` gives the exact rounding error of the product, so
`rint(x*p)` only needs correcting when the product lands exactly on a half.
A test compares it with the decimal path on ~7M values and ties; removing
the tie correction makes it fail. This also explained phase 4's production
scan plateau.

**What did Linux and the production stack catch?**
Two bugs, invisible on the Mac:
- `requirements.txt` pins pandas 2.1.4, which gives an index built from
  datetimes nanosecond resolution; pandas 3 (the local venv) gives
  microseconds. The arrays path hardcoded microseconds, so in production its
  rebuilt DataFrames would have differed. Scores were fine; it's fixed, and
  parity now runs in the production Docker image and in CI on those pins.
- The test binary's allocation counter replaced `operator new` but not the
  `nothrow` form, so gtest's memory came from one allocator and went back
  to another. Linux ASan reports that mismatch; macOS ASan doesn't check it.

**How is production verified?**
`CPP_ENGINE_SHADOW=true` makes Step 4 also run the Python layers on the same
in-memory data and log how many tickers differ, so a deployment checks
itself on that day's real data without fetching twice.

**Benchmarks** (M3 Pro, AC power, Low Power Mode off, Release build)

| threads | backtest, batch indicators | backtest, streaming | full run_backtest | production scan |
|---|---|---|---|---|
| 1 | 34.4 s | 623 ms | 393 ms | 22.7 ms |
| 2 | 17.9 s (1.9x) | 304 ms (2.0x) | 211 ms (1.9x) | 11.8 ms (1.9x) |
| 4 | 9.66 s (3.6x) | 157 ms (4.0x) | 112 ms (3.5x) | 6.37 ms (3.6x) |
| 8 | 6.42 s (5.4x) | 99.5 ms (6.3x) | 75.0 ms (5.2x) | 4.23 ms (5.4x) |
| 11 | 5.44 s (6.3x) | 83.8 ms (7.4x) | 63.5 ms (6.2x) | 3.51 ms (6.5x) |

End to end, Alpaca response to end of Step 4 (single-threaded): Python
10.5 s, C++ via DataFrames 4.6 s, C++ via arrays 0.28 s (38x); plus 0.64 s of
survivor DataFrames after Step 5 on the arrays path.

**What did the backtest find?**
PV-only, top 10 a day, next-open fills, 2023-10-31 to 2026-10-28: positive
returns (+1.35% per 20-day pick) but behind SPY (-0.27% excess, 45% hit
rate) in a window where SPY rose 92%. The bigger finding is that the PV
score saturates: on 542 of 751 days all 10 picks tie at the maximum 3.0 and
the alphabetical tie-break chooses them. Layers 3 to 5 break those ties in
the live scan, so this says the PV part can't rank on its own, not that the
full model fails.
