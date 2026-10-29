"""
backend/tests/validate_universe_fallback.py

Offline checks that a universe refresh never crashes get_universe():
  - An asset on an exchange alpaca-py does not model (TXSE) is skipped and
    logged; the NYSE/Nasdaq assets around it still make the universe
  - The real alpaca-py ValidationError raised by get_all_assets falls back to
    the stale SQLite cache
  - Any other refresh error (network) also falls back to the stale cache
  - With no cache at all, the hardcoded fallback list is returned

No network or Alpaca keys needed: the Alpaca client and price fetch are
mocked and the cache is an in-memory SQLite database.

Run with:
    python -m backend.tests.validate_universe_fallback
"""

import copy
import logging
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend import config
from backend.db.models import Base, UniverseCache
from backend import universe

SEP  = "=" * 68
PASS = "[PASS]"
FAIL = "[FAIL]"

# Shapes copied from a live GET /v2/assets response (2026-10-29)
_ASSET_TEMPLATE = {
    "id": "b0b6dd9d-8b9b-48a9-ba46-b9d54906e415", "class": "us_equity",
    "exchange": "NASDAQ", "symbol": "AAPL", "name": "Apple Inc. Common Stock",
    "status": "active", "tradable": True, "marginable": True,
    "maintenance_margin_requirement": 30, "margin_requirement_long": "30",
    "margin_requirement_short": "30", "shortable": True, "easy_to_borrow": True,
    "borrow_status": "easy_to_borrow", "fractionable": True,
    "attributes": ["fractional_eh_enabled", "has_options"],
}


def _asset(symbol: str, exchange: str, n: int) -> dict:
    a = copy.deepcopy(_ASSET_TEMPLATE)
    a.update(symbol=symbol, exchange=exchange, name=f"{symbol} Inc",
             id=f"00000000-0000-0000-0000-{n:012d}")
    return a


RAW_ASSETS = [
    _asset("AAPL", "NASDAQ", 1),
    _asset("JPM",  "NYSE",   2),
    _asset("THRU", "TXSE",   3),   # alpaca-py 0.43.4 has no AssetExchange.TXSE
    _asset("XYZQ", "TXSE",   4),
    _asset("SPY",  "ARCA",   5),   # valid model, filtered by exchange
]
STALE_TICKERS = ["MSFT", "NVDA"]


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _pf(cond: bool, label: str, detail: str = "") -> bool:
    line = f"  {PASS if cond else FAIL}  {label}"
    if detail:
        line += f"  ({detail})"
    print(line)
    return cond


def _session(seed_stale: bool) -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = Session(engine)
    if seed_stale:
        # naive UTC, matching universe._is_fresh / _rebuild_cache
        old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)
        for t in STALE_TICKERS:
            db.add(UniverseCache(ticker=t, exchange="NASDAQ", name=t,
                                 market_cap=1e9, last_updated=old))
        db.commit()
    return db


def _client_returning(raw=None, error: Exception | None = None) -> MagicMock:
    client = MagicMock()
    if error is not None:
        client.get_all_assets.side_effect = error
    else:
        client.get_all_assets.return_value = raw
    return MagicMock(return_value=client)


def _real_validation_error() -> ValidationError:
    """The exact error alpaca-py raises when it parses a list containing TXSE."""
    from alpaca.trading.models import Asset
    try:
        TypeAdapter(list[Asset]).validate_python(RAW_ASSETS)
    except ValidationError as exc:
        return exc
    raise AssertionError("alpaca-py accepted TXSE -- the enum may have been updated")


def _run(db: Session, client_cls: MagicMock, price_vol=None) -> tuple[list[str], list[str]]:
    price_vol = price_vol or (lambda tickers: {t: (150.0, 5_000_000.0) for t in tickers})
    handler = _ListHandler()
    logger = logging.getLogger("backend.universe")
    logger.addHandler(handler)
    try:
        with patch.object(config, "ALPACA_API_KEY", "test"), \
             patch.object(config, "ALPACA_SECRET_KEY", "test"), \
             patch("alpaca.trading.client.TradingClient", client_cls), \
             patch.object(universe, "_batch_price_vol", side_effect=price_vol):
            tickers = universe.get_universe(db, force_refresh=True)
    finally:
        logger.removeHandler(handler)
    return tickers, handler.messages


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)-8s %(name)s  %(message)s")
    print(f"\n{SEP}\n  Universe refresh fallback validation (offline)\n{SEP}")
    ok = True

    # 1. TXSE assets are skipped, the rest still builds the universe
    print("\n  1. Refresh with TXSE assets in the response")
    db = _session(seed_stale=False)
    tickers, logs = _run(db, _client_returning(raw=RAW_ASSETS))
    ok &= _pf(tickers == ["AAPL", "JPM"], "NYSE/Nasdaq assets kept", str(tickers))
    skip_log = next((m for m in logs if "failed validation" in m), "")
    ok &= _pf("TXSE=2" in skip_log and "THRU" in skip_log, "TXSE skips logged", skip_log)
    cached = sorted(r.ticker for r in db.query(UniverseCache).all())
    ok &= _pf(cached == ["AAPL", "JPM"], "cache rebuilt", str(cached))

    # 2. alpaca-py's own ValidationError -> stale cache
    print("\n  2. get_all_assets raises the alpaca-py ValidationError")
    db = _session(seed_stale=True)
    tickers, logs = _run(db, _client_returning(error=_real_validation_error()))
    ok &= _pf(tickers == STALE_TICKERS, "stale cache returned", str(tickers))
    ok &= _pf(any("Universe refresh failed: ValidationError" in m for m in logs),
              "failure logged")

    # 3. Any other refresh failure -> stale cache
    print("\n  3. Price fetch raises a network error")
    db = _session(seed_stale=True)

    def _boom(_tickers):
        raise ConnectionError("simulated outage")

    tickers, logs = _run(db, _client_returning(raw=RAW_ASSETS), price_vol=_boom)
    ok &= _pf(tickers == STALE_TICKERS, "stale cache returned", str(tickers))
    ok &= _pf(any("ConnectionError" in m for m in logs), "failure logged")

    # 4. No cache at all -> hardcoded fallback
    print("\n  4. Refresh fails and the cache is empty")
    db = _session(seed_stale=False)
    tickers, _ = _run(db, _client_returning(error=_real_validation_error()))
    ok &= _pf(len(tickers) > 100 and "AAPL" in tickers,
              "hardcoded fallback returned", f"{len(tickers)} tickers")

    print(f"\n{SEP}\n  {'ALL PASSED' if ok else 'FAILURES ABOVE'}\n{SEP}\n")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
