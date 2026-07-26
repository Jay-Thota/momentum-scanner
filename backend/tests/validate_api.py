"""
backend/tests/validate_api.py

Starts the FastAPI server in a subprocess, exercises every endpoint, and
asserts correct response shapes. Uses data already in the DB (no new scan).

Run with:
    python -m backend.tests.validate_api
"""

import subprocess
import sys
import time

import requests

from backend import config

BASE    = "http://localhost:8765"
SECRET  = config.WEBHOOK_SECRET
PORT    = 8765
TIMEOUT = 10   # per-request timeout (seconds)
BOOT    = 30   # max seconds to wait for server startup


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def wait_for_server(url: str, timeout: int = BOOT) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(url, timeout=2)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def check(label: str, r: requests.Response, expected_status: int = 200) -> dict:
    if r.status_code != expected_status:
        print(f"FAIL  {label} -- expected HTTP {expected_status}, got {r.status_code}: {r.text[:200]}")
        return {}
    print(f"PASS  {label} -- HTTP {r.status_code}")
    return r.json()


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

def run_tests() -> int:
    """Return number of failures."""
    failures = 0

    # -- /health --
    r = requests.get(f"{BASE}/health", timeout=TIMEOUT)
    body = check("/health", r)
    if body.get("status") != "ok":
        print("FAIL  /health -- missing 'status: ok'"); failures += 1
    if "last_scan" not in body:
        print("FAIL  /health -- missing 'last_scan' key"); failures += 1

    # -- /watchlist/today --
    r = requests.get(f"{BASE}/watchlist/today", timeout=TIMEOUT)
    body = check("/watchlist/today", r)
    if "scan_date" not in body or "results" not in body:
        print("FAIL  /watchlist/today -- unexpected shape"); failures += 1
    else:
        results = body["results"]
        print(f"      scan_date={body['scan_date']}  results={len(results)}")
        if results:
            top = results[0]
            for key in ("ticker", "total_score", "l1", "l2", "l3", "l4", "l5", "signals"):
                if key not in top:
                    print(f"FAIL  /watchlist/today -- missing key '{key}' in result"); failures += 1

    # -- /watchlist/history --
    r = requests.get(f"{BASE}/watchlist/history", timeout=TIMEOUT)
    body = check("/watchlist/history", r)
    if not isinstance(body, list):
        print("FAIL  /watchlist/history -- expected list"); failures += 1
    elif body:
        entry = body[0]
        for key in ("scan_date", "stock_count", "top_ticker"):
            if key not in entry:
                print(f"FAIL  /watchlist/history -- missing key '{key}'"); failures += 1
        print(f"      {len(body)} scan date(s), most recent: {body[0].get('scan_date')}")

    # -- /watchlist/{date} with a known date --
    today_resp = requests.get(f"{BASE}/watchlist/today", timeout=TIMEOUT).json()
    known_date = today_resp.get("scan_date", "")
    if known_date:
        r = requests.get(f"{BASE}/watchlist/{known_date}", timeout=TIMEOUT)
        body = check(f"/watchlist/{known_date}", r)
        if "results" not in body:
            print(f"FAIL  /watchlist/{{date}} -- missing 'results'"); failures += 1

    # -- /watchlist/{date} with a bad date -> 404 --
    r = requests.get(f"{BASE}/watchlist/1999-01-01", timeout=TIMEOUT)
    check("/watchlist/1999-01-01 (expect 404)", r, expected_status=404)

    # -- /stock/{ticker} -- use the top-ranked ticker from today's scan --
    top_ticker = None
    if today_resp.get("results"):
        top_ticker = today_resp["results"][0]["ticker"]
    if top_ticker:
        r = requests.get(f"{BASE}/stock/{top_ticker}", timeout=TIMEOUT)
        body = check(f"/stock/{top_ticker}", r)
        if "appearances" not in body:
            print(f"FAIL  /stock/{top_ticker} -- missing 'appearances'"); failures += 1
        elif body["appearances"]:
            app = body["appearances"][0]
            for key in ("scan_date", "ticker", "total_score", "signals"):
                if key not in app:
                    print(f"FAIL  /stock/{{ticker}} -- missing key '{key}'"); failures += 1

        # -- /stock/{ticker}/options-history --
        r = requests.get(f"{BASE}/stock/{top_ticker}/options-history", timeout=TIMEOUT)
        body = check(f"/stock/{top_ticker}/options-history", r)
        if "history" not in body:
            print(f"FAIL  /stock/{top_ticker}/options-history -- missing 'history'"); failures += 1
        else:
            print(f"      {len(body['history'])} options-history row(s)")

    # -- /stock/{ticker} for unknown ticker -> 404 --
    r = requests.get(f"{BASE}/stock/ZZZZZZ", timeout=TIMEOUT)
    check("/stock/ZZZZZZ (expect 404)", r, expected_status=404)

    # -- /webhook/tradingview with invalid secret -> 401 --
    r = requests.post(
        f"{BASE}/webhook/tradingview",
        json={"ticker": "AAPL", "close": 200.0, "volume": 1e7,
              "l1_score": 0.0, "l2_score": 0.0},
        headers={"X-Webhook-Secret": "wrong-secret"},
        timeout=TIMEOUT,
    )
    check("/webhook/tradingview (bad secret, expect 401)", r, expected_status=401)

    # -- /webhook/tradingview with valid secret --
    # L1+L2=0 so total will be well below threshold regardless of L3-L5;
    # added_to_watchlist should be False. Timeout 90s to allow L4 FINRA fetch.
    print("      Calling /webhook/tradingview (valid secret) -- may take ~20s ...")
    r = requests.post(
        f"{BASE}/webhook/tradingview",
        json={"ticker": "AAPL", "close": 200.0, "volume": 1e7,
              "l1_score": 0.0, "l2_score": 0.0, "archetype": "1"},
        headers={"X-Webhook-Secret": SECRET},
        timeout=90,
    )
    body = check("/webhook/tradingview (valid secret)", r)
    if body:
        for key in ("ticker", "total_score", "added_to_watchlist"):
            if key not in body:
                print(f"FAIL  /webhook/tradingview -- missing key '{key}'"); failures += 1
        if body.get("ticker") != "AAPL":
            print(f"FAIL  /webhook/tradingview -- ticker mismatch"); failures += 1
        print(f"      ticker={body.get('ticker')}  total={body.get('total_score')}  added={body.get('added_to_watchlist')}")

    return failures


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print(f"\nStarting uvicorn on port {PORT} ...")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn",
         "backend.api.app:app", "--port", str(PORT), "--log-level", "warning"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        if not wait_for_server(f"{BASE}/health", timeout=BOOT):
            print("FAIL  Server did not start within timeout")
            proc.terminate()
            sys.exit(1)
        print(f"Server ready at {BASE}\n")

        failures = run_tests()

        print()
        if failures == 0:
            print(f"All tests passed.")
        else:
            print(f"{failures} test(s) FAILED.")
            sys.exit(1)
    finally:
        proc.terminate()
        proc.wait()


if __name__ == "__main__":
    main()
