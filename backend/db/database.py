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

from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from backend import config
from backend.db.models import Base

# Resolve a relative sqlite:/// URL against the project root so the DB file
# ends up at backend/db/scanner.db regardless of the working directory.
_db_url = config.DATABASE_URL
if _db_url.startswith("sqlite:///") and not _db_url.startswith("sqlite:////"):
    _rel = _db_url[len("sqlite:///"):]
    _abs = Path(__file__).resolve().parents[2] / _rel
    _abs.parent.mkdir(parents=True, exist_ok=True)
    _db_url = f"sqlite:///{_abs}"

engine = create_engine(
    _db_url,
    connect_args={"check_same_thread": False},  # safe for single-writer use
    echo=False,
)

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def _migrate() -> None:
    """Additive schema migrations that create_all cannot handle (column additions)."""
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
