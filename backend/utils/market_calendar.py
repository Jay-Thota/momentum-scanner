from datetime import date, timedelta
import pandas_market_calendars as mcal


def get_next_n_trading_days(n: int = 2, exchange: str = "NYSE") -> list[date]:
    """Return the next n NYSE trading days, starting from today (inclusive if a trading day)."""
    cal = mcal.get_calendar(exchange)
    today = date.today()
    end_date = today + timedelta(days=n * 5 + 5)
    schedule = cal.schedule(start_date=str(today), end_date=str(end_date))
    return [idx.date() for idx in schedule.index[:n]]
