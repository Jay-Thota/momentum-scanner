# Momentum Scanner -- Frontend

React/Next.js dashboard for the momentum scanner pipeline.

## Running locally

**Step 1 -- start the backend** (in the project root):

```bash
cd momentum-scanner
source venv/bin/activate
uvicorn backend.api.app:app --reload --port 8000
```

**Step 2 -- start the frontend** (in a second terminal):

```bash
cd frontend
npm run dev
```

Opens at http://localhost:3000

## Pages

- `/` -- Today's watchlist with regime badge, score bars, and archetype labels. Auto-refreshes between 4:00-4:15 PM ET to catch the scan completing.
- `/history` -- All past scan dates; click any row to expand the full watchlist inline.
- `/stock/[ticker]` -- Score breakdown, layer score bar, total-score line chart over time, P/C ratio chart.

## Environment

```
NEXT_PUBLIC_API_URL=http://localhost:8000
```
