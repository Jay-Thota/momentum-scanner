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

## Deployment

### Railway (API + scheduler)

1. Create a new Railway project and connect the GitHub repo.
2. Add a PostgreSQL plugin -- Railway injects `DATABASE_URL` automatically.
3. Set all required environment variables in Railway's dashboard.
4. The `Dockerfile` handles the build. Railway reads the `Procfile` for process types:
   - `web` -- the FastAPI server
   - `scheduler` -- the APScheduler cron process

The scheduler logs `Scheduler armed -- next scan: YYYY-MM-DD 15:30:00 EDT` on startup so you can confirm it is using Eastern time, not UTC.

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
