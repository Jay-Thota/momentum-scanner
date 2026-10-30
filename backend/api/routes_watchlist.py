"""
backend/api/routes_watchlist.py

Endpoints:
    GET /watchlist/today          -- top-10 from today, or most recent scan if no today
    GET /watchlist/history        -- all scan dates with stock count and top ticker
    GET /watchlist/{scan_date}    -- full ranked list for a specific date (YYYY-MM-DD)
"""

from datetime import date
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select

from backend.api.signals import load_signals
from backend.db.database import SessionLocal
from backend.db.models import ScanResult, StockSignals

router = APIRouter(prefix="/watchlist", tags=["watchlist"])


class WatchlistEntry(BaseModel):
    ticker: str
    total_score: float
    price: Optional[float] = None
    l1: Optional[float] = None
    l2: Optional[float] = None
    l3: Optional[float] = None
    l4: Optional[float] = None
    l5: Optional[float] = None
    archetype: Optional[str] = None
    regime: Optional[str] = None
    timestamp: Optional[str] = None
    signals: dict[str, Any] = {}


class WatchlistResponse(BaseModel):
    scan_date: str
    results: list[WatchlistEntry]


class HistoryEntry(BaseModel):
    scan_date: str
    stock_count: int
    top_ticker: Optional[str] = None
    top_score: Optional[float] = None
    regime: Optional[str] = None


def _row_to_entry(row: ScanResult, signal_rows: list[StockSignals]) -> WatchlistEntry:
    return WatchlistEntry(
        ticker=row.ticker,
        total_score=row.total_score,
        price=row.price,
        l1=row.l1,
        l2=row.l2,
        l3=row.l3,
        l4=row.l4,
        l5=row.l5,
        archetype=row.archetype,
        regime=row.regime,
        timestamp=row.timestamp,
        signals=load_signals(signal_rows),
    )


def _load_ranked(session, scan_date: str) -> list[WatchlistEntry]:
    rows = (
        session.execute(
            select(ScanResult)
            .where(ScanResult.scan_date == scan_date)
            .order_by(ScanResult.total_score.desc())
        )
        .scalars()
        .all()
    )
    result = []
    for row in rows:
        sigs = (
            session.execute(
                select(StockSignals).where(StockSignals.scan_result_id == row.id)
            )
            .scalars()
            .all()
        )
        result.append(_row_to_entry(row, sigs))
    return result


@router.get("/today", response_model=WatchlistResponse)
def get_today():
    """Return today's watchlist, falling back to the most recent scan date."""
    today = date.today().isoformat()
    with SessionLocal() as session:
        has_today = session.execute(
            select(func.count(ScanResult.id)).where(ScanResult.scan_date == today)
        ).scalar_one()

        if has_today:
            scan_date = today
        else:
            scan_date = session.execute(
                select(func.max(ScanResult.scan_date))
            ).scalar_one_or_none()
            if scan_date is None:
                return WatchlistResponse(scan_date=today, results=[])

        return WatchlistResponse(scan_date=scan_date, results=_load_ranked(session, scan_date))


@router.get("/history", response_model=list[HistoryEntry])
def get_history():
    """Return a summary row for every scan date in the DB, newest first."""
    with SessionLocal() as session:
        scan_dates = (
            session.execute(
                select(ScanResult.scan_date)
                .distinct()
                .order_by(ScanResult.scan_date.desc())
            )
            .scalars()
            .all()
        )

        history = []
        for scan_date in scan_dates:
            stock_count = session.execute(
                select(func.count(ScanResult.id)).where(ScanResult.scan_date == scan_date)
            ).scalar_one()
            top = session.execute(
                select(ScanResult)
                .where(ScanResult.scan_date == scan_date)
                .order_by(ScanResult.total_score.desc())
                .limit(1)
            ).scalar_one_or_none()
            history.append(HistoryEntry(
                scan_date=scan_date,
                stock_count=stock_count,
                top_ticker=top.ticker      if top else None,
                top_score=top.total_score  if top else None,
                regime=top.regime          if top else None,
            ))

    return history


@router.get("/{scan_date}", response_model=WatchlistResponse)
def get_by_date(scan_date: str):
    """Return the full ranked watchlist for a specific date (YYYY-MM-DD)."""
    with SessionLocal() as session:
        count = session.execute(
            select(func.count(ScanResult.id)).where(ScanResult.scan_date == scan_date)
        ).scalar_one()
        if count == 0:
            raise HTTPException(status_code=404, detail=f"No scan found for {scan_date}")
        return WatchlistResponse(scan_date=scan_date, results=_load_ranked(session, scan_date))
