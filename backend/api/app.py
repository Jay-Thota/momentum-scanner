"""
backend/api/app.py

FastAPI application entry point.

Run with:
    uvicorn backend.api.app:app --reload --port 8000
"""

from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import func, select

from backend.api.routes_stock import router as stock_router
from backend.api.routes_watchlist import router as watchlist_router
from backend.api.routes_webhook import router as webhook_router
from backend.db.database import SessionLocal, init_db
from backend.db.models import ScanResult, UniverseCache


class HealthResponse(BaseModel):
    status: str
    last_scan: Optional[str] = None
    universe_size: int
    regime: Optional[str] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Momentum Scanner API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(watchlist_router)
app.include_router(stock_router)
app.include_router(webhook_router)


@app.get("/health", response_model=HealthResponse, tags=["meta"])
def health():
    """Returns server status, last scan timestamp, universe size, and market regime."""
    from backend.utils.regime_filter import get_regime

    with SessionLocal() as session:
        last_scan = session.execute(
            select(func.max(ScanResult.timestamp))
        ).scalar_one_or_none()
        universe_size = session.execute(
            select(func.count(UniverseCache.id))
        ).scalar_one()

    try:
        regime_str = get_regime()["regime"]
    except Exception:
        regime_str = None

    return HealthResponse(
        status="ok",
        last_scan=last_scan,
        universe_size=universe_size,
        regime=regime_str,
    )
