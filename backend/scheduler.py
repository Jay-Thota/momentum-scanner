"""
backend/scheduler.py

Runs run_daily_scan() every weekday at 3:30 PM ET and fires email + Slack alerts.
Skips non-trading days (NYSE holidays) automatically.

Run standalone:
    python -m backend.scheduler

The scheduler blocks until interrupted (Ctrl+C / SIGTERM).
"""

import logging
import time
import traceback
from datetime import date, datetime
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from backend.alerts.email_alert import send_failure_email, send_watchlist_email
from backend.alerts.slack_alert import send_slack_alert
from backend.db.database import init_db
from backend.scoring.scorer import run_daily_scan
from backend.utils.market_calendar import get_next_n_trading_days
from backend.utils.regime_filter import get_regime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s -- %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def _is_trading_day(today: date | None = None) -> bool:
    """Return True if today is an NYSE trading day (not a holiday or weekend)."""
    today = today or date.today()
    try:
        nxt = get_next_n_trading_days(n=1)
        return bool(nxt and nxt[0] == today)
    except Exception as exc:
        logger.warning("Could not check market calendar: %s -- assuming trading day", exc)
        return True


def run_scan_job(force: bool = False, stats: dict | None = None) -> list:
    """
    Execute the full daily scan and fire alerts.

    Parameters
    ----------
    force : bool
        If True, bypass the market-holiday check. Useful for manual testing.
    stats : dict | None
        If provided, populated with pipeline counters from run_daily_scan.

    Returns
    -------
    list[ScoringResult]
        The watchlist produced by run_daily_scan (empty list on error or skip).
    """
    today = date.today()

    if not force and not _is_trading_day(today):
        logger.info("Scan skipped -- %s is not a trading day", today)
        return []

    logger.info("=== Daily scan starting (%s) ===", today)
    t0 = time.perf_counter()

    _stats: dict = {} if stats is None else stats
    results: list = []

    try:
        t_scan = time.perf_counter()
        results = run_daily_scan(stats=_stats)
        logger.info(
            "Scan phase: %.1fs -- %d in watchlist  (%d scanned)",
            time.perf_counter() - t_scan,
            len(results),
            _stats.get("universe_size", 0),
        )
    except Exception as exc:
        tb = traceback.format_exc()
        logger.error("Daily scan failed: %s", exc, exc_info=True)
        try:
            send_failure_email(tb, stats=_stats)
            logger.info("Failure email sent")
        except Exception as mail_exc:
            logger.warning("Failure email also failed: %s", mail_exc)
        return []

    # -- Regime for alert context --
    try:
        t_regime = time.perf_counter()
        regime = get_regime()
        logger.info("Regime fetch: %.1fs  regime=%s", time.perf_counter() - t_regime, regime["regime"])
    except Exception as exc:
        logger.warning("get_regime failed for alert: %s", exc)
        regime = {"regime": "unknown"}

    # -- Email --
    try:
        t_email = time.perf_counter()
        send_watchlist_email(results, regime, _stats)
        logger.info("Email alert: %.1fs", time.perf_counter() - t_email)
    except Exception as exc:
        logger.warning("Email alert failed: %s", exc)

    # -- Slack --
    try:
        t_slack = time.perf_counter()
        send_slack_alert(results, regime, _stats)
        logger.info("Slack alert: %.1fs", time.perf_counter() - t_slack)
    except Exception as exc:
        logger.warning("Slack alert failed: %s", exc)

    logger.info("=== Daily scan job finished -- total %.1fs ===", time.perf_counter() - t0)
    return results


# ---------------------------------------------------------------------------
# Entry point -- blocking scheduler
# ---------------------------------------------------------------------------

def main() -> None:
    init_db()
    scheduler = BlockingScheduler(timezone="America/New_York")

    trigger = CronTrigger(
        day_of_week="mon-fri",
        hour=15,
        minute=30,
        timezone="America/New_York",
    )
    scheduler.add_job(
        run_scan_job,
        trigger=trigger,
        id="daily_scan",
        name="Daily 3:30 PM ET momentum scan",
        misfire_grace_time=300,   # fire within 5 minutes of missed trigger
        coalesce=True,            # run at most once if multiple misfires stack up
    )

    logger.info("Scheduler started -- daily scan fires Mon-Fri at 15:30 America/New_York")

    # Compute next fire time directly from the trigger before scheduler.start() blocks.
    # (Accessing job.next_run_time before start() raises AttributeError in APScheduler 3.x)
    now_et    = datetime.now(_ET)
    next_fire = trigger.get_next_fire_time(None, now_et)
    if next_fire:
        logger.info(
            "Scheduler armed -- next scan: %s",
            next_fire.astimezone(_ET).strftime("%Y-%m-%d %H:%M:%S %Z"),
        )
    else:
        logger.warning("Scheduler armed -- could not determine next fire time")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped")


if __name__ == "__main__":
    main()
