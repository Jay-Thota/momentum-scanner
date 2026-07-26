"""
backend/alerts/slack_alert.py

Posts the daily watchlist summary to a Slack channel via an Incoming Webhook.

Config keys required (.env):
    SLACK_WEBHOOK_URL   Slack Incoming Webhook URL
                        Leave empty to disable Slack alerts.
"""

import logging
from datetime import date

import requests

from backend import config

logger = logging.getLogger(__name__)

_ARCH_LABELS: dict[int | str, str] = {
    1: "Uptrend",    "1": "Uptrend",
    2: "Recovery",   "2": "Recovery",
    3: "Breakout",   "3": "Breakout",
    4: "Rotation",   "4": "Rotation",
}


def send_slack_alert(
    results: list,
    regime: dict | None = None,
    stats: dict | None = None,
) -> None:
    """
    Send a Slack alert with the top-3 watchlist tickers.

    Skips silently if SLACK_WEBHOOK_URL is not configured.
    Logs a warning on HTTP error but does not raise.
    """
    if not config.SLACK_WEBHOOK_URL:
        logger.info("Slack alert skipped -- SLACK_WEBHOOK_URL not configured")
        return

    today      = date.today().strftime("%Y-%m-%d")
    regime_str = (regime.get("regime") if regime else None) or "unknown"
    n_results  = len(results)
    n_scanned  = stats.get("universe_size", "?") if stats else "?"
    elapsed    = f"{stats.get('elapsed_s', '?')}s" if stats else "?"

    # Top-3 detail lines
    top3_lines = []
    for rank, r in enumerate(results[:3], 1):
        ticker = r.get("ticker", "?") if isinstance(r, dict) else getattr(r, "ticker", "?")
        total  = r.get("total_score", 0.0) if isinstance(r, dict) else getattr(r, "total", 0.0)
        arch   = r.get("archetype") if isinstance(r, dict) else getattr(r, "archetype", None)
        arch_label = _ARCH_LABELS.get(arch, "--")
        top3_lines.append(f"{rank}. *{ticker}*  `{total:.2f}`  {arch_label}")

    top3_text = "\n".join(top3_lines) if top3_lines else "_No qualifying stocks today_"

    header_text = f"Momentum Scanner -- {today}"
    summary = (
        f"*Regime:* {regime_str.upper()}  |  *{n_results} stock{'s' if n_results != 1 else ''} qualified*\n\n"
        f"*Top {min(3, n_results)}:*\n{top3_text}"
    )
    footer = f"Scanned {n_scanned} tickers in {elapsed}"

    payload = {
        "text": header_text,
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": header_text, "emoji": False},
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": summary},
            },
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": footer}],
            },
        ],
    }

    try:
        resp = requests.post(
            config.SLACK_WEBHOOK_URL,
            json=payload,
            timeout=10,
        )
        if resp.status_code == 200 and resp.text == "ok":
            logger.info("Slack alert sent -- %d results, regime=%s", n_results, regime_str)
        else:
            logger.warning(
                "Slack alert HTTP %d: %s", resp.status_code, resp.text[:200]
            )
    except Exception as exc:
        logger.warning("Slack alert failed: %s", exc)
