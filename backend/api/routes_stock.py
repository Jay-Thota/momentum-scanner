"""
backend/api/routes_stock.py

Endpoints:
    GET /stock/{ticker}                  -- all scan appearances in last 30 days
    GET /stock/{ticker}/options-history  -- last 10 OptionsHistory rows
"""

from datetime import date, timedelta
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from backend.api.signals import load_signals
from backend.db.database import SessionLocal
from backend.db.models import MetadataCache, OptionsHistory, ScanResult, StockSignals

router = APIRouter(prefix="/stock", tags=["stock"])


class StockAppearance(BaseModel):
    scan_date: str
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


class StockMeta(BaseModel):
    sector: Optional[str] = None
    etf_symbol: Optional[str] = None
    si_pct: Optional[float] = None
    days_to_cover: Optional[float] = None


class StockResponse(BaseModel):
    ticker: str
    appearances: list[StockAppearance]
    meta: StockMeta = StockMeta()


class OptionsHistoryEntry(BaseModel):
    scan_date: str
    put_call_ratio: Optional[float] = None
    call_vol_oi_ratio: Optional[float] = None
    iv_slope: Optional[float] = None


class OptionsHistoryResponse(BaseModel):
    ticker: str
    history: list[OptionsHistoryEntry]


def _row_to_appearance(row: ScanResult, signal_rows: list[StockSignals]) -> StockAppearance:
    return StockAppearance(
        scan_date=row.scan_date,
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


@router.get("/{ticker}/options-history", response_model=OptionsHistoryResponse)
def get_options_history(ticker: str):
    """Return the last 10 OptionsHistory rows for a ticker."""
    ticker = ticker.upper()
    with SessionLocal() as session:
        rows = (
            session.execute(
                select(OptionsHistory)
                .where(OptionsHistory.ticker == ticker)
                .order_by(OptionsHistory.scan_date.desc())
                .limit(10)
            )
            .scalars()
            .all()
        )
    return OptionsHistoryResponse(
        ticker=ticker,
        history=[
            OptionsHistoryEntry(
                scan_date=r.scan_date,
                put_call_ratio=r.put_call_ratio,
                call_vol_oi_ratio=r.call_vol_oi_ratio,
                iv_slope=r.iv_slope,
            )
            for r in rows
        ],
    )


@router.get("/{ticker}", response_model=StockResponse)
def get_ticker(ticker: str):
    """Return all scan appearances for a ticker in the last 30 days."""
    ticker = ticker.upper()
    cutoff = (date.today() - timedelta(days=30)).isoformat()
    with SessionLocal() as session:
        rows = (
            session.execute(
                select(ScanResult)
                .where(ScanResult.ticker == ticker)
                .where(ScanResult.scan_date >= cutoff)
                .order_by(ScanResult.scan_date.desc())
            )
            .scalars()
            .all()
        )
        if not rows:
            raise HTTPException(
                status_code=404,
                detail=f"{ticker} has no scan appearances in the last 30 days",
            )
        appearances = []
        for row in rows:
            sigs = (
                session.execute(
                    select(StockSignals).where(StockSignals.scan_result_id == row.id)
                )
                .scalars()
                .all()
            )
            appearances.append(_row_to_appearance(row, sigs))

        meta_row = session.execute(
            select(MetadataCache).where(MetadataCache.ticker == ticker)
        ).scalar_one_or_none()

    meta = StockMeta()
    if meta_row is not None:
        meta = StockMeta(
            sector=meta_row.sector,
            etf_symbol=meta_row.etf_symbol,
            si_pct=meta_row.si_pct,
            days_to_cover=meta_row.days_to_cover,
        )

    return StockResponse(ticker=ticker, appearances=appearances, meta=meta)
