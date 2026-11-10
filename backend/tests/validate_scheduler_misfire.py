"""
backend/tests/validate_scheduler_misfire.py

Offline checks of the scan job's misfire policy (no scan, no alerts, no
network). The job function is replaced with a recorder; everything else is
the scheduler code main() uses (add_scan_job, start_wakeup_thread).

  1. The job carries config.SCAN_MISFIRE_GRACE_SECONDS and coalesce=True
  2. A run time 2 h in the past still runs (inside the 3 h grace)
  3. A run time 4 h in the past is skipped as missed
  4. Several overdue run times inside the grace run once (coalesce)
  5. A scheduler whose timed wait overshoots the due time (what a sleeping
     Mac does to it) only runs the job on time with the wakeup thread

Run with:
    python -m backend.tests.validate_scheduler_misfire
"""

import sys
import threading
import time
from datetime import datetime, timedelta

from apscheduler.events import EVENT_JOB_MISSED
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from backend import config
from backend.scheduler import _ET, add_scan_job, start_wakeup_thread

failures = 0


def check(ok: bool, msg: str) -> None:
    global failures
    print(f"  [{'PASS' if ok else 'FAIL'}]  {msg}")
    failures += not ok


class Recorder:
    def __init__(self) -> None:
        self.calls = 0
        self.ran = threading.Event()

    def __call__(self) -> None:
        self.calls += 1
        self.ran.set()


def run_with(trigger, scheduler_cls=BackgroundScheduler, wait: float = 2.0):
    """Start a scheduler with the scan job on `trigger`; return (calls, missed events)."""
    rec, missed = Recorder(), []
    sched = scheduler_cls(timezone=_ET)
    sched.add_listener(lambda e: missed.append(e), EVENT_JOB_MISSED)
    sched.start()
    add_scan_job(sched, trigger, func=rec)
    rec.ran.wait(wait)
    time.sleep(0.2)   # let the scheduler finish with the job before shutting down
    sched.shutdown(wait=False)
    return rec.calls, len(missed)


class OvershootingScheduler(BackgroundScheduler):
    """Waits an hour between checks however soon the next job is due, like a
    timed wait that stopped counting while the machine slept."""

    def _process_jobs(self):
        super()._process_jobs()
        return 3600.0


def main() -> None:
    grace = config.SCAN_MISFIRE_GRACE_SECONDS
    now = datetime.now(_ET)
    print(f"SCAN_MISFIRE_GRACE_SECONDS={grace} ({grace / 3600:.1f} h)  "
          f"SCHEDULER_WAKEUP_SECONDS={config.SCHEDULER_WAKEUP_SECONDS}")

    sched = BackgroundScheduler(timezone=_ET)
    job = add_scan_job(sched, DateTrigger(now + timedelta(days=1)), func=Recorder())
    check(job.misfire_grace_time == grace and job.coalesce is True,
          f"job misfire_grace_time={job.misfire_grace_time}, coalesce={job.coalesce}")

    calls, missed = run_with(DateTrigger(now - timedelta(seconds=grace - 3600)))
    check(calls == 1 and missed == 0, f"due {(grace - 3600) / 3600:.0f} h ago: ran {calls}x, missed {missed}")

    calls, missed = run_with(DateTrigger(now - timedelta(seconds=grace + 3600)), wait=1.0)
    check(calls == 0 and missed == 1, f"due {(grace + 3600) / 3600:.0f} h ago: ran {calls}x, missed {missed}")

    # Run times every 0.5 s on a scheduler that next looks in an hour; waking
    # it after 3 s finds ~6 overdue run times inside the grace: one run
    rec = Recorder()
    sched = OvershootingScheduler(timezone=_ET)
    sched.start()
    add_scan_job(sched, IntervalTrigger(seconds=0.5, timezone=_ET), func=rec)
    time.sleep(3.0)
    sched.wakeup()
    rec.ran.wait(2.0)
    time.sleep(0.2)
    sched.shutdown(wait=False)
    check(rec.calls == 1, f"~6 stacked misfires inside the grace: ran {rec.calls}x (coalesced)")

    # Job due in 1 s on a scheduler that next looks in an hour
    due = DateTrigger(datetime.now(_ET) + timedelta(seconds=1))
    calls, _ = run_with(due, OvershootingScheduler, wait=3.0)
    check(calls == 0, f"overshooting wait, no wakeup thread: ran {calls}x within 3 s (the bug)")

    rec = Recorder()
    sched = OvershootingScheduler(timezone=_ET)
    start_wakeup_thread(sched, 0.5)
    sched.start()
    t0 = time.monotonic()
    add_scan_job(sched, DateTrigger(datetime.now(_ET) + timedelta(seconds=1)), func=rec)
    rec.ran.wait(5.0)
    elapsed = time.monotonic() - t0
    time.sleep(0.2)   # let the scheduler finish removing the one-off job
    sched.shutdown(wait=False)
    check(rec.calls == 1, f"overshooting wait, wakeup every 0.5 s: ran {rec.calls}x "
                          f"after {elapsed:.1f} s (due after 1 s)")

    print()
    if failures:
        print(f"{failures} check(s) FAILED")
        sys.exit(1)
    print("ALL PASSED")


if __name__ == "__main__":
    main()
