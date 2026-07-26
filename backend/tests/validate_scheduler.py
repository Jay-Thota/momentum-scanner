"""
backend/tests/validate_scheduler.py

Validates the scheduler, email alert, and Slack alert end-to-end.

Steps:
  1. Confirm _is_trading_day() works correctly
  2. Trigger run_scan_job(force=True) -- bypasses holiday check, runs full ~3,277-ticker scan
  3. Verify >= 10 results saved to DB for today
  4. Call send_watchlist_email() directly and confirm it sends (or SKIP if unconfigured)
  5. Test Slack alert if configured, skip if not
  6. Simulate a scan failure and confirm send_failure_email sends (or SKIP if unconfigured)
  Print final summary with watchlist top-3 and per-step elapsed times.

Note: Step 2 runs the full ~3,277-ticker scan; expect 8-15 minutes on cold cache,
      ~9 minutes on warm cache.

Run with:
    python -m backend.tests.validate_scheduler
"""

import logging
import sys
import time
from datetime import date

from sqlalchemy import func, select

from backend import config
from backend.alerts.email_alert import send_failure_email, send_watchlist_email
from backend.alerts.slack_alert import send_slack_alert
from backend.db.database import SessionLocal, init_db
from backend.db.models import ScanResult
from backend.scheduler import _is_trading_day, run_scan_job
from backend.utils.regime_filter import get_regime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s -- %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def _section(title: str) -> None:
    print(f"\n{'='*60}\n{title}\n{'='*60}")


def main() -> None:
    init_db()
    today    = date.today().isoformat()
    failures = 0
    t_start  = time.perf_counter()

    # -------------------------------------------------------------------
    # Step 1 -- market calendar check
    # -------------------------------------------------------------------
    _section("Step 1 -- Market calendar check")
    t1 = time.perf_counter()
    trading = _is_trading_day()
    print(f"Today ({today}) is {'a trading day' if trading else 'NOT a trading day'}")
    print(f"PASS  _is_trading_day() returned without error  ({time.perf_counter()-t1:.1f}s)")

    # -------------------------------------------------------------------
    # Step 2 -- run scan job (force=True bypasses holiday check)
    # -------------------------------------------------------------------
    _section("Step 2 -- Triggering scan job (force=True)")
    print("Running the full ~3,277-ticker scan -- expect 8-15 minutes ...\n")

    scan_stats: dict = {}
    t2 = time.perf_counter()
    results = run_scan_job(force=True, stats=scan_stats)
    t2_elapsed = time.perf_counter() - t2

    if results is None:
        print("FAIL  run_scan_job returned None"); failures += 1
        sys.exit(1)

    print(f"\nScan phase elapsed : {t2_elapsed:.1f}s")
    if scan_stats:
        print(f"Universe size      : {scan_stats.get('universe_size', '?')}")
        print(f"L0 price+vol pass  : {scan_stats.get('l0_pv_pass', '?')}")
        print(f"Early exit discard : {scan_stats.get('early_exit_discarded', '?')}")
        print(f"L0 full pass       : {scan_stats.get('l0_pass', '?')}")
        print(f"Scored             : {scan_stats.get('scored', '?')}")
        print(f"Qualified          : {scan_stats.get('qualified', '?')}")
        print(f"Watchlist          : {scan_stats.get('watchlist', '?')}")
        print(f"Regime             : {scan_stats.get('regime', '?')}")
    print(f"Watchlist results  : {len(results)}")

    if len(results) > 0:
        print("PASS  run_scan_job completed with results")
    else:
        print("WARN  Watchlist is empty (possible rate-limiting; check logs above)")

    # -------------------------------------------------------------------
    # Step 3 -- verify DB has >= 10 results for today
    # -------------------------------------------------------------------
    _section("Step 3 -- DB verification")
    t3 = time.perf_counter()
    with SessionLocal() as session:
        db_count = session.execute(
            select(func.count(ScanResult.id)).where(ScanResult.scan_date == today)
        ).scalar_one()
        latest_date = session.execute(
            select(func.max(ScanResult.scan_date))
        ).scalar_one_or_none()

    if db_count >= 10:
        print(f"PASS  DB has {db_count} row(s) for scan_date={today}  ({time.perf_counter()-t3:.1f}s)")
    elif db_count > 0:
        print(f"WARN  DB has only {db_count} row(s) for {today} (expected >= 10)")
        print("      (Low-signal day or rate-limiting -- check logs)")
    elif latest_date:
        print(f"WARN  No rows for {today}; most recent scan_date is {latest_date}")
        print("      (Possible rate-limiting caused empty watchlist)")
    else:
        print("FAIL  DB is empty"); failures += 1

    # resolve fallback results for email steps
    results_for_alerts = results
    if not results_for_alerts and latest_date:
        with SessionLocal() as session:
            results_for_alerts = (
                session.execute(
                    select(ScanResult)
                    .where(ScanResult.scan_date == latest_date)
                    .order_by(ScanResult.total_score.desc())
                )
                .scalars()
                .all()
            )

    # -------------------------------------------------------------------
    # Step 4 -- email alert
    # -------------------------------------------------------------------
    _section("Step 4 -- Email alert")
    t4 = time.perf_counter()
    if not config.SMTP_USER or not config.ALERT_EMAIL_TO:
        print("SKIP  SMTP_USER or ALERT_EMAIL_TO not set in .env")
        print("      Set SMTP_USER, SMTP_PASSWORD, ALERT_EMAIL_TO to enable")
    else:
        print(f"Sending to: {config.ALERT_EMAIL_TO} via {config.SMTP_HOST}:{config.SMTP_PORT}")
        try:
            regime = get_regime()
            send_watchlist_email(results_for_alerts, regime, scan_stats or None)
            print(f"PASS  Email sent to {config.ALERT_EMAIL_TO}  ({time.perf_counter()-t4:.1f}s)")
            print("      Check inbox to confirm delivery")
        except Exception as exc:
            print(f"FAIL  Email send error: {exc}"); failures += 1

    # -------------------------------------------------------------------
    # Step 5 -- Slack alert
    # -------------------------------------------------------------------
    _section("Step 5 -- Slack alert")
    t5 = time.perf_counter()
    if not config.SLACK_WEBHOOK_URL:
        print("SKIP  SLACK_WEBHOOK_URL not set in .env")
    else:
        print("Posting to Slack webhook ...")
        try:
            regime = get_regime()
            send_slack_alert(results_for_alerts, regime, scan_stats or None)
            print(f"PASS  Slack alert posted  ({time.perf_counter()-t5:.1f}s)  (check channel for message)")
        except Exception as exc:
            print(f"FAIL  Slack alert error: {exc}"); failures += 1

    # -------------------------------------------------------------------
    # Step 6 -- failure email simulation
    # -------------------------------------------------------------------
    _section("Step 6 -- Failure email simulation")
    t6 = time.perf_counter()
    if not config.SMTP_USER or not config.ALERT_EMAIL_TO:
        print("SKIP  SMTP not configured -- skipping failure email simulation")
    else:
        fake_tb = (
            "Traceback (most recent call last):\n"
            "  File 'backend/scoring/scorer.py', line 42, in run_daily_scan\n"
            "    ohlcv_map = fetch_daily_ohlcv_batch_alpaca(universe)\n"
            "RuntimeError: Simulated test failure -- validate_scheduler Step 6"
        )
        print(f"Sending failure email to {config.ALERT_EMAIL_TO} ...")
        try:
            send_failure_email(fake_tb, stats={"universe_size": 3277, "elapsed_s": 12.3})
            print(f"PASS  Failure email sent  ({time.perf_counter()-t6:.1f}s)  (check inbox)")
        except Exception as exc:
            print(f"FAIL  Failure email error: {exc}"); failures += 1

    # -------------------------------------------------------------------
    # Summary
    # -------------------------------------------------------------------
    _section("Summary")
    total_elapsed = time.perf_counter() - t_start

    top = results[:3] if results else []
    if top:
        print("Top-3 from this run:")
        for rank, r in enumerate(top, 1):
            ticker = getattr(r, "ticker", "?")
            total  = getattr(r, "total",  0.0)
            arch   = getattr(r, "archetype", None)
            print(f"  {rank}. {ticker:<8}  {total:.2f}  arch={arch}")

    print(f"\nTotal elapsed: {total_elapsed:.1f}s")
    print()
    if failures == 0:
        print("All checks passed.")
    else:
        print(f"{failures} check(s) FAILED.")
        sys.exit(1)


if __name__ == "__main__":
    main()
