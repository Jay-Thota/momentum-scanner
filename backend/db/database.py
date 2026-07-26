"""
backend/db/database.py

SQLAlchemy engine, session factory, and table initialisation.

Usage:
    from backend.db.database import get_db, init_db, SessionLocal

    init_db()                       # call once at startup (idempotent)

    with SessionLocal() as session: # direct session
        ...

    for session in get_db():        # generator form (FastAPI / DI compatible)
        ...
"""

import os
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from backend import config
from backend.db.models import Base

# Railway PostgreSQL URLs start with postgres:// but SQLAlchemy needs postgresql://
DATABASE_URL = os.getenv("DATABASE_URL", config.DATABASE_URL)
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Resolve a relative sqlite:/// URL against the project root so the DB file
# ends up at the project root regardless of the working directory.
_db_url = DATABASE_URL
if _db_url.startswith("sqlite:///") and not _db_url.startswith("sqlite:////"):
    _rel = _db_url[len("sqlite:///"):]
    _abs = Path(__file__).resolve().parents[2] / _rel
    _abs.parent.mkdir(parents=True, exist_ok=True)
    _db_url = f"sqlite:///{_abs}"

_is_sqlite = _db_url.startswith("sqlite://")
engine = create_engine(
    _db_url,
    connect_args={"check_same_thread": False} if _is_sqlite else {},
    echo=False,
)

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def _migrate() -> None:
    """Additive schema migrations that create_all cannot handle (column additions).
    SQLite-only: PostgreSQL uses create_all with checkfirst=True, which is sufficient."""
    if not _is_sqlite:
        return
    with engine.connect() as conn:
        scan_cols = {
            row[1]
            for row in conn.execute(text("PRAGMA table_info(scan_results)")).fetchall()
        }
        if "price" not in scan_cols:
            conn.execute(text("ALTER TABLE scan_results ADD COLUMN price REAL"))
            conn.commit()

        meta_cols = {
            row[1]
            for row in conn.execute(text("PRAGMA table_info(metadata_cache)")).fetchall()
        }
        for col, typedef in (("eps_growth", "REAL"), ("revenue_growth", "REAL")):
            if col not in meta_cols:
                conn.execute(text(f"ALTER TABLE metadata_cache ADD COLUMN {col} {typedef}"))
        conn.commit()


def init_db() -> None:
    """Create all tables if they do not already exist, then apply migrations."""
    Base.metadata.create_all(engine)
    _migrate()


def get_db():
    """Generator-style session factory (compatible with FastAPI Depends)."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
