"""
backend/alerts/email_alert.py

Sends the post-scan watchlist as an HTML email via Gmail SMTP.

Config keys required (.env):
    SMTP_HOST        default smtp.gmail.com
    SMTP_PORT        default 587 (STARTTLS)
    SMTP_USER        Gmail address used to authenticate
    SMTP_PASSWORD    Gmail app password (not account password)
    ALERT_EMAIL_FROM sender address (defaults to SMTP_USER)
    ALERT_EMAIL_TO   recipient address
"""

import html as _html
import logging
import smtplib
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import yfinance as yf

from backend import config

logger = logging.getLogger(__name__)

_ARCH_LABELS: dict[int | str, str] = {
    1: "Uptrend",    "1": "Uptrend",
    2: "Recovery",   "2": "Recovery",
    3: "Breakout",   "3": "Breakout",
    4: "Rotation",   "4": "Rotation",
}

_YAHOO_ALIAS: dict[str, str] = {"BRK.B": "BRK-B", "BF.B": "BF-B"}


def _f(r, *keys, default=None):
    """Get a field from either a dict or an object, trying multiple key names."""
    for k in keys:
        if isinstance(r, dict):
            if k in r:
                return r[k]
        else:
            v = getattr(r, k, None)
            if v is not None:
                return v
    return default


# ---------------------------------------------------------------------------
# Price fetch
# ---------------------------------------------------------------------------

def _fetch_prices(tickers: list[str]) -> dict[str, float | None]:
    """Batch-fetch the latest closing price for a list of tickers."""
    if not tickers:
        return {}
    syms = [_YAHOO_ALIAS.get(t, t) for t in tickers]
    prices: dict[str, float | None] = {t: None for t in tickers}
    try:
        if len(syms) == 1:
            df = yf.download(syms[0], period="2d", progress=False, auto_adjust=True)
            if not df.empty:
                prices[tickers[0]] = round(float(df["Close"].iloc[-1]), 2)
        else:
            df = yf.download(syms, period="2d", progress=False, auto_adjust=True)
            if not df.empty:
                close_row = df["Close"].iloc[-1]
                for orig, sym in zip(tickers, syms):
                    try:
                        prices[orig] = round(float(close_row[sym]), 2)
                    except Exception:
                        pass
    except Exception as exc:
        logger.debug("_fetch_prices failed: %s", exc)
    return prices


# ---------------------------------------------------------------------------
# HTML builder
# ---------------------------------------------------------------------------

_SCORE_GREEN = "#28a745"
_SCORE_BLUE  = "#0d6efd"
_SCORE_AMBER = "#fd7e14"
_SCORE_GREY  = "#6c757d"

_TABLE_HEADER_BG = "#343a40"

_CSS_TABLE = "border-collapse:collapse;width:100%;font-size:13px;font-family:Arial,sans-serif;"
_CSS_TH    = "padding:8px 10px;text-align:left;color:#fff;"
_CSS_TD    = "padding:7px 10px;border-bottom:1px solid #dee2e6;"
_CSS_PILL  = "border-radius:3px;padding:1px 5px;font-size:11px;font-weight:bold;color:#fff;display:inline-block;margin:1px"


def _score_color(total: float) -> str:
    if total >= 5.0:
        return _SCORE_GREEN
    if total >= 4.0:
        return _SCORE_BLUE
    if total >= 3.0:
        return _SCORE_AMBER
    return _SCORE_GREY


def _pill(value: float | None, label: str) -> str:
    if value is None:
        return f'<span style="{_CSS_PILL}background:{_SCORE_GREY}">{label}--</span>'
    color = _SCORE_GREEN if value >= 0.75 else (_SCORE_BLUE if value >= 0.5 else _SCORE_AMBER if value >= 0.25 else _SCORE_GREY)
    return f'<span style="{_CSS_PILL}background:{color}">{label}{value:.2f}</span>'


def _build_html(
    results: list,
    regime: dict,
    stats: dict | None,
    prices: dict[str, float | None],
    today: str,
) -> str:
    regime_str  = (regime.get("regime") or "unknown").upper()
    spx_price   = regime.get("spx_price")
    spx_200ma   = regime.get("spx_200ma")
    n_scanned   = stats.get("universe_size", "?") if stats else "?"
    n_scored    = stats.get("scored", "?")         if stats else "?"
    elapsed     = f"{stats.get('elapsed_s', '?')}s" if stats else "?"

    spx_line = ""
    if spx_price is not None and spx_200ma is not None:
        spx_line = f"SPX {spx_price:,.2f} vs 200MA {spx_200ma:,.2f} &bull; "

    regime_color = "#28a745" if regime_str == "BULL" else "#dc3545"

    # -- table rows --
    rows_html = ""
    for rank, r in enumerate(results, 1):
        ticker = _f(r, "ticker",                    default="?")
        total  = _f(r, "total", "total_score",      default=0.0)
        l1     = _f(r, "l1",                        default=None)
        l2     = _f(r, "l2",                        default=None)
        l3     = _f(r, "l3",                        default=None)
        l4     = _f(r, "l4",                        default=None)
        l5     = _f(r, "l5",                        default=None)
        arch   = _f(r, "archetype",                 default=None)
        price  = prices.get(ticker)

        arch_label = _ARCH_LABELS.get(arch, "--")
        color      = _score_color(total)
        bg         = "#f8f9fa" if rank % 2 == 0 else "#ffffff"
        price_str  = f"${price:,.2f}" if price is not None else "--"

        pills = (
            _pill(l1, "L1:") + " " +
            _pill(l2, "L2:") + " " +
            _pill(l3, "L3:") + " " +
            _pill(l4, "L4:") + " " +
            _pill(l5, "L5:")
        )

        rows_html += (
            f'<tr style="background:{bg}">'
            f'<td style="{_CSS_TD}text-align:center">{rank}</td>'
            f'<td style="{_CSS_TD}font-weight:bold">{ticker}</td>'
            f'<td style="{_CSS_TD}font-weight:bold;color:{color}">{total:.2f}</td>'
            f'<td style="{_CSS_TD}">{pills}</td>'
            f'<td style="{_CSS_TD}">{arch_label}</td>'
            f'<td style="{_CSS_TD}">{price_str}</td>'
            f'</tr>'
        )

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:Arial,sans-serif;max-width:720px;margin:auto;padding:20px;color:#212529">
  <h2 style="margin-bottom:4px">Momentum Scanner</h2>
  <p style="margin-top:0;font-size:14px;color:#6c757d">
    {today} &bull;
    Regime: <strong style="color:{regime_color}">{regime_str}</strong> &bull;
    {spx_line}Universe: {n_scanned} tickers &bull; Scored: {n_scored} &bull; Elapsed: {elapsed}
  </p>

  <table style="{_CSS_TABLE}">
    <tr style="background:{_TABLE_HEADER_BG}">
      <th style="{_CSS_TH}width:36px">#</th>
      <th style="{_CSS_TH}">Ticker</th>
      <th style="{_CSS_TH}">Score</th>
      <th style="{_CSS_TH}">Layer Scores</th>
      <th style="{_CSS_TH}">Archetype</th>
      <th style="{_CSS_TH}">Price</th>
    </tr>
    {rows_html}
  </table>

  <hr style="margin:20px 0;border:none;border-top:1px solid #dee2e6">
  <p style="font-size:11px;color:#adb5bd">
    Score legend: <span style="color:{_SCORE_GREEN}">&#9632;</span> &ge;5.0 &nbsp;
    <span style="color:{_SCORE_BLUE}">&#9632;</span> &ge;4.0 (watchlist threshold) &nbsp;
    <span style="color:{_SCORE_AMBER}">&#9632;</span> &ge;3.0 &nbsp; &bull; &nbsp;
    Powered by free data stack (Alpaca + yfinance + FINRA)
  </p>
</body></html>"""


# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------

def send_watchlist_email(
    results: list,
    regime: dict,
    stats: dict | None = None,
) -> None:
    """
    Send the daily watchlist as an HTML email.

    Skips silently if SMTP_USER or ALERT_EMAIL_TO are not configured.
    Raises on SMTP authentication failure so the caller can log the error.
    """
    if not config.SMTP_USER or not config.ALERT_EMAIL_TO:
        logger.info("Email alert skipped -- SMTP_USER or ALERT_EMAIL_TO not configured")
        return

    today = date.today().strftime("%Y-%m-%d")
    n     = len(results)
    regime_str = (regime.get("regime") or "unknown").capitalize()

    subject = (
        f"Momentum Scanner -- {today} -- {n} stock{'s' if n != 1 else ''}"
        f" | Regime: {regime_str}"
    )

    tickers = [_f(r, "ticker", default="?") for r in results]
    prices  = _fetch_prices(tickers)
    html    = _build_html(results, regime, stats, prices, today)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = config.ALERT_EMAIL_FROM or config.SMTP_USER
    msg["To"]      = config.ALERT_EMAIL_TO
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(config.SMTP_USER, config.SMTP_PASSWORD)
        smtp.sendmail(msg["From"], [config.ALERT_EMAIL_TO], msg.as_string())

    logger.info(
        "Watchlist email sent to %s -- subject: %s",
        config.ALERT_EMAIL_TO, subject,
    )


def send_failure_email(error_msg: str, stats: dict | None = None) -> None:
    """
    Send a plain-HTML failure alert with the traceback.

    Skips silently if SMTP_USER or ALERT_EMAIL_TO are not configured.
    """
    if not config.SMTP_USER or not config.ALERT_EMAIL_TO:
        logger.info("Failure email skipped -- SMTP_USER or ALERT_EMAIL_TO not configured")
        return

    today   = date.today().strftime("%Y-%m-%d")
    subject = f"Momentum Scanner FAILED -- {today}"

    safe_tb = _html.escape(error_msg)
    extra   = ""
    if stats:
        extra = (
            f"<p style='font-size:12px;color:#6c757d'>"
            f"Universe scanned: {stats.get('universe_size', '?')} &bull; "
            f"Elapsed before failure: {stats.get('elapsed_s', '?')}s"
            f"</p>"
        )

    html_body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:Arial,sans-serif;max-width:720px;margin:auto;padding:20px;color:#212529">
  <h2 style="color:#dc3545">Momentum Scanner FAILED -- {today}</h2>
  {extra}
  <pre style="background:#f8f9fa;padding:16px;border-radius:4px;font-size:12px;overflow:auto;white-space:pre-wrap">{safe_tb}</pre>
  <p style="font-size:11px;color:#adb5bd">Powered by free data stack (Alpaca + yfinance + FINRA)</p>
</body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = config.ALERT_EMAIL_FROM or config.SMTP_USER
    msg["To"]      = config.ALERT_EMAIL_TO
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(config.SMTP_USER, config.SMTP_PASSWORD)
        smtp.sendmail(msg["From"], [config.ALERT_EMAIL_TO], msg.as_string())

    logger.info("Failure email sent to %s", config.ALERT_EMAIL_TO)
