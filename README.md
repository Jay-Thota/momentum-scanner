# Momentum Scanner

A daily pipeline that scans ~3,300 NYSE and Nasdaq equities at 3:30 PM ET and produces a ranked top-10 watchlist of stocks showing institutional accumulation signals. Free data sources only.

## How it works

The scanner runs in two stages each trading day:

**Stage 1 -- Universe construction** (cached 7 days)
Alpaca Markets free tier fetches all active NYSE/Nasdaq equities, then filters to ~3,300 tickers with price >= $5 and 5-day average volume >= 200K.

**Stage 2 -- Daily scan** (runs at 3:30 PM ET, Mon-Fri)
Each ticker in the universe is scored across five layers. Tickers need >= 4.0 / 6.5 points to appear on the watchlist. The top 10 by score are saved to the database and sent via email and Slack.

### Scoring layers

| Layer | Max pts | Signal |
|---|---|---|
| L0 | pass/fail | Price >= $10, volume >= 1M, market cap >= $2B, no earnings within 2 days |
| L1 | 1.0 | Volume surge -- RVOL, OBV trend, block trade detection |
| L2 | 2.0 | Price structure -- uptrend, early recovery, breakout, sector rotation archetypes + ADX/ROC momentum |
| L3 | 1.0 | Options flow -- call/put ratio, call vol/OI ratio, IV slope |
| L4 | 1.0 | Short interest gate -- hard disqualifies on SI% >= 10%, DTC >= 3, or rising short trend |
| L5 | 1.5 | Sector and macro alignment -- sector ETF momentum, RS rank, bull/bear regime filter |

In a bear regime the minimum score is raised to 5.0.

## Tech stack

- **Backend** -- Python 3.11, FastAPI, SQLAlchemy, APScheduler
- **Engine** -- C++20 scoring library and backtester, pybind11 bindings, GoogleTest, Google Benchmark
- **Data** -- Alpaca Markets (OHLCV), yfinance (fundamentals, options, short interest), FINRA SSHO files (short interest)
- **Database** -- SQLite locally, PostgreSQL on Railway
- **Frontend** -- Next.js 15, TypeScript, Tailwind CSS, TradingView chart widget
- **Deployment** -- Railway (API + scheduler), Vercel (frontend)

## Project structure

```
backend/
  api/            FastAPI routes (watchlist, stock detail, webhook)
  alerts/         Email (Gmail SMTP) and Slack alert senders
  data/           Fetchers: Alpaca OHLCV, yfinance options/sector, FINRA short files
  db/             SQLAlchemy models, metadata cache, database init
  scoring/        Layer 0-5 scorers and the main run_daily_scan() pipeline
  tests/          Validation scripts for each layer (run with python -m backend.tests.validate_layerN)
  utils/          Market calendar, regime filter
  config.py       All thresholds loaded from environment variables
  scheduler.py    APScheduler cron job + alert dispatch
  universe.py     Universe construction and SQLite cache

engine/
  include/mscan/  Universe (mmap), indicator kernels, streaming indicators, layers, thread pool, backtest
  src/            Implementations
  python/         pybind11 module mscan_engine
  tests/          GoogleTest suites + golden files dumped from the Python layers
  bench/          Google Benchmark suite
  NOTES.md        Design decisions, phase by phase

scripts/          History export, golden dumps, Step 4 timing, run_backtest.py

frontend/
  app/            Next.js App Router pages (today, history, stock detail)
  components/     WatchlistTable, StockModal, ScoreBar, RegimeBadge, etc.
  lib/            API client, TypeScript types
```

## Local setup

**Prerequisites:** Python 3.11, Node.js 18+, a free [Alpaca Markets](https://alpaca.markets) account.

```bash
git clone https://github.com/Jay-Thota/momentum-scanner.git
cd momentum-scanner

# Backend
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env and fill in ALPACA_API_KEY, ALPACA_SECRET_KEY, and email/Slack fields

# Frontend
cd frontend
npm install
```

### Required environment variables

Copy `.env.example` to `.env` and fill in:

| Variable | Description |
|---|---|
| `ALPACA_API_KEY` | Alpaca free-tier API key |
| `ALPACA_SECRET_KEY` | Alpaca secret key |
| `ALERT_EMAIL_FROM` | Gmail address to send alerts from |
| `ALERT_EMAIL_TO` | Email address to receive alerts |
| `SMTP_PASSWORD` | Gmail App Password (not your account password) |
| `SLACK_WEBHOOK_URL` | Slack incoming webhook URL (optional) |
| `WEBHOOK_SECRET` | Secret for TradingView webhook endpoint |
| `DATABASE_URL` | Defaults to `sqlite:///./scanner.db` |

All scoring thresholds (`MIN_PRICE`, `MIN_AVG_VOLUME`, `WATCHLIST_MIN_SCORE`, etc.) have working defaults and can be left unset.

## Running locally

```bash
# Run a scan immediately (outputs watchlist to console + DB)
source venv/bin/activate
python -m backend.scoring.scorer

# Start the API server
python -m uvicorn backend.api.app:app --reload --port 8000

# Start the scheduler (fires daily at 3:30 PM ET, market days only)
python -m backend.scheduler

# Start the frontend dev server
cd frontend && npm run dev
```

Frontend runs at `http://localhost:3000`, API at `http://localhost:8000`.

### Validate each layer individually

```bash
python -m backend.tests.validate_universe   # confirm ~3,300 tickers, $5 price floor
python -m backend.tests.validate_layer0    # L0 gate
python -m backend.tests.validate_layer1    # volume surge
python -m backend.tests.validate_layer2    # price structure
python -m backend.tests.validate_layer3    # options flow
python -m backend.tests.validate_layer4    # short interest
python -m backend.tests.validate_layer5    # sector alignment
python -m backend.tests.validate_full_scan # end-to-end pipeline
```

## API endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Status, last scan timestamp, universe size, regime |
| GET | `/watchlist/today` | Latest scan results |
| GET | `/watchlist/history` | List of past scan dates |
| GET | `/watchlist/{date}` | Results for a specific scan date |
| GET | `/stock/{ticker}` | All appearances for a ticker + metadata |
| POST | `/webhook/tradingview` | TradingView alert ingestion (HMAC-verified) |

## C++ engine

Layers 1 and 2 (volume surge and price structure) need only daily OHLCV, so they also exist as a C++20 library, `engine/`. It does two jobs: it scores the daily scan's Step 4 when `USE_CPP_ENGINE=true`, and it powers a historical backtester. Python keeps everything that needs the network (fetching, market cap and earnings, options, short interest, sectors) plus the API, database and alerts. Design notes, phase by phase: [engine/NOTES.md](engine/NOTES.md).

### Architecture

```
Alpaca raw JSON --> OhlcvBatch (T x D arrays) --+--> mscan_engine.score_l1l2_batch  (daily scan, Step 4)
                                                |
history.bin (4 years, mmap) --------------------+--> mscan_engine.run_backtest       (scripts/run_backtest.py)
                                                       |
                         Universe --> StreamingIndicators --> score_layer1/2 --> picks --> forward returns, stats
                                      (ThreadPool, one chunk of tickers per task)
```

- **The Python layers are the reference.** The C++ reproduces them bit for bit, quirks included: pandas' Kahan-compensated rolling mean, the rolling-mean (not Wilder) ADX, and Python's and numpy's two different `round()`s. Parity is checked against golden files dumped from Python, against the real 3,289-ticker universe, and on the production pins (Python 3.11, pandas 2.1.4) in Docker and CI. With `CPP_ENGINE_SHADOW=true`, a live scan also runs the Python path on the same data and logs any difference.
- **No look-ahead.** Anything scoring day d sees only `Universe::series(i, d)`. A test replaces every bar after d with garbage and requires the picks through d to be unchanged.
- **No heap allocation in indicator kernels**; streaming state is about 6 KB of fixed arrays per ticker.

### Data layout

`engine/data/history.bin` (written by `scripts/export_history.py`) is a 40-byte header, int32 YYYYMMDD dates, NUL-terminated tickers, then five float64 `[ticker][day]` arrays (open, high, low, close, volume), 8-byte aligned. The engine mmaps it and reads the arrays in place, so opening the 132 MB file costs nothing up front. Each ticker's row is NaN before its first bar, and `series(i, d)` is a set of `std::span`s from that first bar to day d. The daily scan builds the same layout in memory straight from Alpaca's JSON and hands it over without copying.

### Threading model

A fixed `ThreadPool` (`std::jthread` workers, one mutex-guarded queue, futures carry results and exceptions). `parallel_for` splits tickers into about 4 chunks per thread, so threads that finish early pick up more. Workers write only their own tickers' slots, so there are no locks in the hot path and results are identical for any thread count. The Python bindings release the GIL; workers never touch Python objects. Tests run under ThreadSanitizer, and AddressSanitizer + UBSan with leak checking.

### Benchmarks

Apple M3 Pro (5 performance + 6 efficiency cores), AC power, Release build. 3,289 tickers, 1,003 days.

| threads | backtest, recompute indicators daily | backtest, streaming indicators | full `run_backtest` | production scan (Step 4) |
|---|---|---|---|---|
| 1 | 34.4 s | 623 ms | 393 ms | 22.7 ms |
| 4 | 9.66 s | 157 ms | 112 ms | 6.37 ms |
| 11 | 5.44 s | 83.8 ms | 63.5 ms | 3.51 ms |

Streaming indicators make the backtest linear in days instead of quadratic (55x on one thread, same scores bit for bit). End to end, from Alpaca's response to the end of Step 4, the scan takes 10.5 s on the Python path and 0.28 s with the engine (single-threaded, network excluded). The win comes mostly from skipping per-ticker DataFrames.

Reproduce with `python -m scripts.bench_step4` and the `mscan_bench` target (commands in [engine/NOTES.md](engine/NOTES.md) and `.claude/CLAUDE.md`).

### Backtest

```bash
python -m scripts.export_history          # once: 4 years of bars -> engine/data/history.bin
python -m scripts.run_backtest            # add --fill close, --no-regime, --picks-csv picks.csv
```

PV-only score (layer 1 + layer 2, max 3.0), Step 2's price/volume gate, top 10 a day with score >= 2.0 (2.5 in a bear regime), entry at the next open, 2023-10-31 to 2026-10-28 (SPY +92%):

| horizon | picks | hit rate vs SPY | mean return | mean excess vs SPY | Sharpe | max drawdown |
|---|---|---|---|---|---|---|
| 5d | 7,451 | 47.1% | +0.17% | -0.20% | 0.50 | 23.5% |
| 10d | 7,399 | 45.9% | +0.67% | -0.12% | 1.02 | 21.8% |
| 20d | 7,293 | 44.9% | +1.35% | -0.27% | 1.01 | 18.5% |

The picks made money but trailed SPY in a strong market. The more useful finding: the PV score saturates. On 542 of 751 days all ten picks tie at the 3.0 maximum (a median of 16 tickers score 3.0 each day), so the alphabetical tie-break chooses them. Taking every 3.0 scorer instead gives +0.05% 20-day excess, about flat. In the live scan, layers 3 to 5 add up to 3.5 more points and break most of those ties. So this shows the price/volume layers cannot rank on their own; it does not test the full model.

### Limitations

- **PV-only.** Options flow (L3), short interest (L4), market cap and earnings dates (L0), and sector ETF trend / RS rank (L5) have no free history, so the backtest scores only the price/volume part with its own threshold. The regime filter uses SPY in place of ^GSPC.
- **Survivorship bias.** The universe is the June 2026 one. Stocks that delisted or fell out of the filters earlier are missing, so results lean optimistic. It also includes ETFs that pass the filters (T-bill funds like SHV and SGOV score a perfect 3.0 on steady drift).
- **Adjusted vs raw bars.** The history file uses split- and dividend-adjusted bars. The live scan requests Alpaca's default raw bars, so a split inside its lookback looks like a one-day crash to layers 1 and 2; the backtest never sees that.
- **Aligned calendar.** 80 of 3,289 tickers have days with no bar; the backtest scores them with NaN on those days, while the live scan only sees the bars that exist.
- **Costs.** No commissions, spread or slippage; fills at the open or close price.

## Deployment

### Railway (API + scheduler)

1. Create a new Railway project and connect the GitHub repo.
2. Add a PostgreSQL plugin -- Railway injects `DATABASE_URL` automatically.
3. Set all required environment variables in Railway's dashboard.
4. The `Dockerfile` handles the build. Railway reads the `Procfile` for process types:
   - `web` -- the FastAPI server
   - `scheduler` -- the APScheduler cron process

The scheduler logs `Scheduler armed -- next scan: YYYY-MM-DD 15:30:00 EDT` on startup so you can confirm it is using Eastern time, not UTC.

The Dockerfile also builds the C++ engine. Set `USE_CPP_ENGINE=true` to score Step 4 with it; add `CPP_ENGINE_SHADOW=true` for a scan or two to log a Python-vs-C++ comparison on the day's real data (it adds the Python path's ~15 s), then remove it.

### Vercel (frontend)

1. Import the repo into Vercel and set the root directory to `frontend`.
2. Set `NEXT_PUBLIC_API_URL` to your Railway API URL.
3. After deploying, lock CORS in `backend/api/app.py` by replacing `allow_origins=["*"]` with your Vercel URL.

### macOS background scheduler (launchd)

A launchd plist is included to run the scheduler as a persistent background process without a terminal:

```bash
bash scripts/start_scheduler.sh   # load and start
bash scripts/stop_scheduler.sh    # unload
bash scripts/check_scheduler.sh   # status + recent log lines
```

The plist is at `~/Library/LaunchAgents/com.momentumscanner.plist`. Logs write to `logs/scheduler.log` and `logs/scheduler_error.log`.

## Data sources and cost

Everything runs on free tiers:

| Source | Used for | Cost |
|---|---|---|
| Alpaca Markets (IEX feed) | Daily OHLCV for all ~3,300 tickers | Free |
| yfinance | Fundamentals, options chain, short interest (cached 7 days) | Free |
| FINRA SSHO files | Bi-monthly short interest files | Free |
| TradingView | Chart widget in the frontend (existing subscription) | Free widget |

Yahoo Finance calls are batched and cached aggressively (7-day TTL for good data, 1-day TTL for ETFs and timeouts) to stay within rate limits. The cold-start metadata fetch uses 5 concurrent workers with a 5-second per-ticker timeout.
