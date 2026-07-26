"""
backend/db/models.py

SQLAlchemy ORM models for the momentum scanner database.
"""

from datetime import datetime

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class ScanResult(Base):
    __tablename__ = "scan_results"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    scan_date   = Column(String(10), nullable=False, index=True)
    ticker      = Column(String(10), nullable=False, index=True)
    total_score = Column(Float,      nullable=False)
    price       = Column(Float)
    l1          = Column(Float)
    l2          = Column(Float)
    l3          = Column(Float)
    l4          = Column(Float)
    l5          = Column(Float)
    archetype   = Column(String(8))
    regime      = Column(String(8))
    timestamp   = Column(String(30))

    signals = relationship(
        "StockSignals",
        back_populates="scan_result",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint("scan_date", "ticker", name="uq_scan_ticker"),
    )


class StockSignals(Base):
    __tablename__ = "stock_signals"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    scan_result_id = Column(Integer, ForeignKey("scan_results.id"), nullable=False, index=True)
    signal_key     = Column(String(8), nullable=False)
    signal_value   = Column(Text)   # JSON string

    scan_result = relationship("ScanResult", back_populates="signals")


class MetadataCache(Base):
    """
    7-day TTL cache for slow-changing per-ticker metadata fetched from yf.info.
    Centralises all yf.Ticker(t).info calls so the rest of the pipeline never
    calls Yahoo for metadata directly.

    si_pct / days_to_cover are included here because Yahoo reports them from
    the same .info payload and they change only at bi-monthly settlement windows.
    FINRA daily files still provide the real-time trend signal.
    """
    __tablename__ = "metadata_cache"

    id                 = Column(Integer, primary_key=True, autoincrement=True)
    ticker             = Column(String(10), unique=True, nullable=False, index=True)
    market_cap         = Column(Float)
    sector             = Column(String(64))
    etf_symbol         = Column(String(10))
    earnings_next_date = Column(String(12))   # ISO date e.g. "2026-09-05"
    si_pct             = Column(Float)         # shortPercentOfFloat (decimal, 0.10 = 10%)
    days_to_cover      = Column(Float)         # shortRatio
    eps_growth         = Column(Float)         # 1.0=True 0.0=False None=unavailable
    revenue_growth     = Column(Float)         # fractional YoY revenue growth
    last_updated       = Column(String(30))    # ISO datetime


class UniverseCache(Base):
    """
    7-day TTL cache for the NYSE+Nasdaq tradable universe (>= $500M market cap).
    The whole table is rebuilt on each refresh; last_updated is a true DateTime.
    market_cap is stored so validate scripts can verify the floor is working.
    """
    __tablename__ = "universe_cache"

    id           = Column(Integer, primary_key=True)
    ticker       = Column(String, unique=True, nullable=False, index=True)
    exchange     = Column(String)
    name         = Column(String)
    market_cap   = Column(Float)
    last_updated = Column(DateTime, default=datetime.utcnow)


class OptionsHistory(Base):
    __tablename__ = "options_history"

    id                = Column(Integer, primary_key=True, autoincrement=True)
    ticker            = Column(String(10), nullable=False, index=True)
    scan_date         = Column(String(10), nullable=False)
    put_call_ratio    = Column(Float)
    call_vol_oi_ratio = Column(Float)
    iv_slope          = Column(Float)

    __table_args__ = (
        UniqueConstraint("ticker", "scan_date", name="uq_opts_ticker_date"),
    )
