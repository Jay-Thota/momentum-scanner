"""
backend/api/routes_webhook.py

Endpoint:
    POST /webhook/tradingview

Accepts a TradingView alert payload containing pre-computed L1/L2 scores,
re-runs Layers 3-5 server-side, and saves the result to the DB if the
total meets the minimum score threshold.

Securing the endpoint:
    Set WEBHOOK_SECRET in .env. TradingView alerts must include
    the header:  X-Webhook-Secret: <your-secret>
"""

import logging
from typing import Optional

import yfinance as yf
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from backend import config
from backend.data.fetcher_options import fetch_options_signals, fetch_vwap
from backend.data.fetcher_sector import (
    build_sector_returns,
    compute_sector_rs_rank,
    fetch_sector_data,
    get_stock_sector_etf,
)
from backend.data.fetcher_short import download_finra_ssho, get_short_trend
from backend.scoring import (
    layer3_options as layer3,
    layer4_short as layer4,
    layer5_sector as layer5,
)
from backend.scoring.scorer import ScoringResult, save_results_to_db
from backend.utils.regime_filter import get_regime

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook", tags=["webhook"])

_YAHOO_ALIAS: dict[str, str] = {"BRK.B": "BRK-B", "BF.B": "BF-B"}


class TradingViewPayload(BaseModel):
    ticker:    str
    close:     float
    volume:    float
    l1_score:  float
    l2_score:  float
    archetype: str | None = None


class WebhookResponse(BaseModel):
    ticker: str
    total_score: float
    added_to_watchlist: bool
    detail: Optional[str] = None


def _get_short_info(ticker: str) -> tuple[float | None, float | None]:
    """Return (si_pct, days_to_cover) from yfinance for a single ticker."""
    yahoo_sym = _YAHOO_ALIAS.get(ticker, ticker)
    try:
        info = yf.Ticker(yahoo_sym).info
        si  = info.get("shortPercentOfFloat")
        dtc = info.get("shortRatio")
        return (float(si) if si is not None else None,
                float(dtc) if dtc is not None else None)
    except Exception as exc:
        logger.debug("_get_short_info %s: %s", ticker, exc)
        return None, None


@router.post("/tradingview", response_model=WebhookResponse)
def tradingview_webhook(
    payload: TradingViewPayload,
    x_webhook_secret: str | None = Header(default=None),
):
    """
    Receive a TradingView alert, run Layers 3-5, and conditionally add the
    ticker to the watchlist in the DB.
    """
    if x_webhook_secret != config.WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid or missing webhook secret")

    ticker = payload.ticker.upper()
    logger.info("Webhook received: %s  L1=%.2f  L2=%.2f", ticker, payload.l1_score, payload.l2_score)

    # -- Layer 4 (hard gate -- check before spending time on L3/L5) --
    si_pct, days_to_cover = _get_short_info(ticker)
    current_finra = download_finra_ssho(offset_days=0)
    prev_finra    = download_finra_ssho(offset_days=10)
    si_trend      = get_short_trend(ticker, current_finra, prev_finra)

    l4_bundle = {
        "ticker":        ticker,
        "si_pct":        si_pct,
        "days_to_cover": days_to_cover,
        "si_trend":      si_trend,
    }
    l4_score = layer4.score(l4_bundle)

    if l4_score is None:
        logger.info("Webhook %s: L4 hard-disqualified", ticker)
        return WebhookResponse(ticker=ticker, total_score=0.0, added_to_watchlist=False,
                               detail="Layer 4 disqualified")

    # -- Layer 3 --
    opts     = fetch_options_signals(ticker)
    vwap     = fetch_vwap(ticker)
    l3_result = layer3.score({
        "ticker":          ticker,
        "options_signals": opts,
        "last_price":      payload.close,
        "vwap":            vwap,
    })

    # -- Layer 5 --
    regime     = get_regime()
    sector_etf = get_stock_sector_etf(ticker)
    etf_data   = fetch_sector_data(sector_etf) if sector_etf else None
    peer_rets  = build_sector_returns(sector_etf) if sector_etf else {}
    rs_rank    = compute_sector_rs_rank(ticker, peer_rets)

    l5_result = layer5.score({
        "ticker":     ticker,
        "sector_etf": sector_etf,
        "etf_data":   etf_data,
        "rs_rank":    rs_rank,
    }, regime)

    total = round(
        payload.l1_score + payload.l2_score
        + l3_result["score"] + l4_score + l5_result["score"],
        2,
    )

    min_score = (
        config.BEAR_REGIME_MIN_SCORE
        if regime["regime"] == "bear"
        else config.WATCHLIST_MIN_SCORE
    )
    added = total >= min_score

    if added:
        from datetime import datetime
        result = ScoringResult(
            ticker    = ticker,
            total     = total,
            l1        = payload.l1_score,
            l2        = payload.l2_score,
            l3        = l3_result["score"],
            l4        = l4_score,
            l5        = l5_result["score"],
            archetype = str(payload.archetype) if payload.archetype is not None else None,
            signals   = {
                "l1": {"webhook": True, "score": payload.l1_score},
                "l2": {"webhook": True, "score": payload.l2_score, "archetype": payload.archetype},
                "l3": l3_result["signals"],
                "l4": {"si_pct": si_pct, "days_to_cover": days_to_cover,
                       "si_trend": si_trend, "bonus": l4_score},
                "l5": l5_result["signals"],
            },
            timestamp = datetime.now().isoformat(),
            min_score = min_score,
        )
        try:
            save_results_to_db([result], regime["regime"])
        except Exception as exc:
            logger.warning("Webhook DB save failed for %s: %s", ticker, exc)

    logger.info(
        "Webhook %s: total=%.2f  added=%s  (L3=%.1f L4=%.2f L5=%.2f)",
        ticker, total, added, l3_result["score"], l4_score, l5_result["score"],
    )
    return WebhookResponse(ticker=ticker, total_score=total, added_to_watchlist=added)
