# Momentum Scanner -- Project Context

## What this is
A Python pipeline that scans ~3,277 NYSE+Nasdaq equities daily at 3PM ET and produces
a ranked top-10 watchlist of stocks showing institutional accumulation signals.

## Data stack (all free)
- Alpaca Markets free tier (IEX feed) -- daily OHLCV (primary source), universe price/volume filter
- yfinance -- fundamentals, options chain, short interest (MetadataCache only; NOT for OHLCV)
- FINRA SSHO files -- bi-monthly short interest
- TradingView premium (existing subscription) -- Pine Script + webhook bridge

## Universe construction
- Alpaca assets endpoint: 7,870 NYSE+Nasdaq tradable alpha-only tickers
- Price filter (Alpaca 5d close >= $5) + volume filter (5d avg volume >= 200K scaled)
- Result: ~3,277 tickers cached to SQLite for 7 days
- universe.py: get_universe(db, force_refresh=False) -> list[str]
- No Yahoo Finance calls during universe refresh (Alpaca only)

## Scoring system (max 6.5 pts, need >= 4.0 to make watchlist)
- Layer 0: Universe filter (pass/fail) -- price, volume, market cap, earnings gate
- Layer 1: Volume surge (max 1.0 pt) -- RVOL, OBV, block trades
- Layer 2: Price structure & momentum (max 2.0 pts) -- 4 archetypes + ADX/ROC checks
- Layer 3: Options flow partial (max 1.0 pt) -- yfinance chain only; sweeps/dark pool N/A
- Layer 4: Short interest gate (max 1.0 pt) -- FINRA + yfinance; hard disqualifier
- Layer 5: Sector & macro alignment (max 1.5 pts) -- sector ETFs + regime filter

## Build order
We build and validate one layer at a time. Do not proceed to the next layer
until the current validation script passes.

## Key rules for all code
- Never use em dashes -- use double hyphens or restructure
- All thresholds come from config.py (loaded from .env) -- no magic numbers in scoring files
- Each layer returns a score AND a signals dict for debugging
- Validation scripts go in backend/tests/ and run via: python -m backend.tests.validate_layerN
- Use ThreadPoolExecutor for all batch data fetching (15 workers max)
- Log reasons when Layer 0 rejects a ticker and when Layer 4 hard-disqualifies

## C++ engine (engine/) -- in progress
C++20 library + pybind11 module `mscan_engine`. Replaces the Step 4 L1+L2 loop in
scorer.py (pure OHLCV, no network) and powers a historical backtester. Python keeps
fetching, L0 mcap/earnings, L3, L4, L5, API, DB, alerts, scheduler.

### Engine rules
- Python layers in backend/scoring/ are the reference implementation. C++ must match
  them exactly (parity tests vs engine/tests/golden/), including quirks like the
  rolling-mean ADX and the redundant L1 A/B signals. Behavior fixes are separate commits.
- C++ thresholds come from a ScoringConfig passed in from config.py -- no hardcoded
  values in engine/src/ beyond defaults that mirror config.py.
- No heap allocation inside indicator kernels. Callers own buffers.
- Anything that scores "as of day d" only receives universe.series(i, d). Never read
  past d (look-ahead bias). No date.today() in anything the backtester touches.
- Keep the Python Step 4 path behind USE_CPP_ENGINE (default off) so both paths run.
- Thread pool workers never touch Python objects; bindings release the GIL.
- I am learning C++ for this project. When you write C++, briefly explain non-obvious
  choices (memory, ownership, concurrency) in your reply.
- Small commits, one concern each.

### Engine commands
- One-time: venv/bin/pip install pybind11 (the CMake build asks the venv's Python for it)
- Build: cmake -S engine -B engine/build -DPython_EXECUTABLE=$PWD/venv/bin/python && cmake --build engine/build
  (or activate the venv first; add -DMSCAN_PYTHON=OFF to skip the Python module)
- Python module: venv/bin/pip install -e engine (rerun after C++ changes); enable with USE_CPP_ENGINE=true
- Step 4 parity (Python vs C++): python -m backend.tests.validate_cpp_engine
- Step 4 timing: python -m scripts.bench_step4
- Tests: ctest --test-dir engine/build
- Sanitizers: add -DMSCAN_SANITIZE=ON (ASan+UBSan) or -DMSCAN_TSAN=ON
- Benchmarks: engine/build/mscan_bench

## Current status
[x] Layer 0 -- Universe filter
[x] Layer 1 -- Volume surge
[x] Layer 2 -- Price structure
[x] Layer 3 -- Options flow (partial)
[x] Layer 4 -- Short interest gate
[x] Layer 5 -- Sector & macro alignment
[x] scorer.py -- full pipeline
[x] backend/db/ -- SQLAlchemy database layer (ScanResult, StockSignals, OptionsHistory)
[x] backend/api/ -- FastAPI REST API (watchlist, stock, webhook routes)
[x] backend/alerts/ -- email (Gmail SMTP) + Slack (incoming webhook) alerts
[x] backend/scheduler.py -- APScheduler cron at 3PM ET, market holiday aware
[x] Engine phase 1 -- scripts/export_history.py + scripts/dump_golden.py
[x] Engine phase 2 -- Universe (mmap) + indicator kernels + parity tests
[x] Engine phase 3 -- score_layer1/2 + pybind11 + USE_CPP_ENGINE swap in Step 4
[ ] Engine phase 4 -- ThreadPool + Google Benchmark vs Python Step 4
[ ] Engine phase 5 -- StreamingIndicators + backtester + CI + README