import logging
from datetime import date

from backend.config import MIN_PRICE, MIN_AVG_VOLUME, MIN_MARKET_CAP
from backend.utils.market_calendar import get_next_n_trading_days

logger = logging.getLogger(__name__)


_MIN_OHLCV_ROWS = 200  # need ~200 trading days for 50-day MA + RS lookback


def check(ticker: str, data_bundle: dict) -> bool:
    """
    Layer 0 universe filter. Returns True if the ticker passes all gates.

    data_bundle keys:
        price         -- current price (float)
        avg_volume    -- 20-day average daily volume (float)
        market_cap    -- market cap in USD (float)
        earnings_date -- date of next earnings, or None
        ohlcv_rows    -- number of OHLCV rows available (int, optional)
    """
    price: float = data_bundle.get("price", 0.0)
    avg_volume: float = data_bundle.get("avg_volume", 0.0)
    market_cap: float = data_bundle.get("market_cap", 0.0)
    earnings_date: date | None = data_bundle.get("earnings_date")
    ohlcv_rows: int | None = data_bundle.get("ohlcv_rows")

    if ohlcv_rows is not None and ohlcv_rows < _MIN_OHLCV_ROWS:
        logger.debug(
            "SKIP %s: insufficient_history (%d rows < %d)",
            ticker, ohlcv_rows, _MIN_OHLCV_ROWS,
        )
        return False

    if price < MIN_PRICE:
        logger.debug("REJECT %s: price %.2f < %.2f", ticker, price, MIN_PRICE)
        return False

    if avg_volume < MIN_AVG_VOLUME:
        logger.debug("REJECT %s: avg_volume %,.0f < %,.0f", ticker, avg_volume, MIN_AVG_VOLUME)
        return False

    if market_cap < MIN_MARKET_CAP:
        logger.debug("REJECT %s: market_cap %,.0f < %,.0f", ticker, market_cap, MIN_MARKET_CAP)
        return False

    if earnings_date is not None:
        next_2 = set(get_next_n_trading_days(n=2))
        if earnings_date in next_2:
            logger.debug("REJECT %s: earnings on %s within next 2 trading days", ticker, earnings_date)
            return False

    return True
