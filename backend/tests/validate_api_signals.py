"""
backend/tests/validate_api_signals.py

Offline checks that the API returns layer 1 signal C as a real boolean for
rows stored before and after the signal C fix:
  - A legacy row (C stored as the string "True"/"False") comes back as a bool
  - A new row (C stored as a JSON boolean) comes back unchanged
  - Other string signals (regime, tier, breakout_date) are left alone
  - Both /watchlist/{scan_date} and /stock/{ticker} normalize

No network or server needed: the routes run through FastAPI's TestClient
against an in-memory SQLite database.

Run with:
    python -m backend.tests.validate_api_signals
"""

import json
import sys
from datetime import date
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api.app import app
from backend.db.models import Base, ScanResult, StockSignals

SEP  = "=" * 68
PASS = "[PASS]"
FAIL = "[FAIL]"

TODAY = date.today().isoformat()

# Signal payloads as the scorer stored them, before and after the fix
_LEGACY = {
    "l1": '{"A": true, "B": true, "C": "False", "up_down_ratio": 1.12}',
    "l2": '{"archetype": 1.0, "arch_info": {"breakout_date": "2026-10-20"}}',
    "l3": '{"tier": "partial"}',
    "l5": '{"regime": "bull", "sector_etf": "XLK"}',
}
_NEW = {
    "l1": '{"A": true, "B": false, "C": true, "up_down_ratio": 1.61}',
}


def _session_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as s:
        for ticker, score, payload in (("LEGACY", 4.5, _LEGACY), ("NEWROW", 4.2, _NEW)):
            row = ScanResult(scan_date=TODAY, ticker=ticker, total_score=score)
            s.add(row)
            s.flush()
            for key, value in payload.items():
                s.add(StockSignals(scan_result_id=row.id, signal_key=key, signal_value=value))
        s.commit()
    return Session


def main() -> None:
    print(SEP)
    print("  API signal normalization (offline)")
    print(SEP)
    Session = _session_factory()
    failures = 0

    def check(label: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        print(f"  {PASS if ok else FAIL}  {label}{'  ' + detail if detail and not ok else ''}")
        failures += not ok

    with patch("backend.api.routes_watchlist.SessionLocal", Session), \
         patch("backend.api.routes_stock.SessionLocal", Session):
        client = TestClient(app)

        r = client.get(f"/watchlist/{TODAY}")
        check("/watchlist/{scan_date} HTTP 200", r.status_code == 200, r.text[:200])
        by_ticker = {e["ticker"]: e["signals"] for e in r.json().get("results", [])}

        legacy, new = by_ticker.get("LEGACY", {}), by_ticker.get("NEWROW", {})
        check("legacy C 'False' -> false", legacy.get("l1", {}).get("C") is False, json.dumps(legacy.get("l1")))
        check("new C true stays true", new.get("l1", {}).get("C") is True, json.dumps(new.get("l1")))
        check("other strings untouched",
              legacy.get("l5") == {"regime": "bull", "sector_etf": "XLK"}
              and legacy.get("l3") == {"tier": "partial"}
              and legacy.get("l2", {}).get("arch_info") == {"breakout_date": "2026-10-20"},
              json.dumps(legacy))

        r = client.get("/stock/LEGACY")
        check("/stock/{ticker} HTTP 200", r.status_code == 200, r.text[:200])
        apps = r.json().get("appearances", [])
        check("/stock/{ticker} legacy C -> false",
              bool(apps) and apps[0]["signals"]["l1"]["C"] is False, json.dumps(apps)[:200])

    print(SEP)
    print("  ALL PASSED" if failures == 0 else f"  {failures} FAILED")
    print(SEP)
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
