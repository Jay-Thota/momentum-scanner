"""
Layer 3: Options Flow (partial -- free signals only).

Sub-signals B and C (sweeps, dark pool) are hardcoded False; they require
a paid data feed and are left as stubs for future extension.
Score 1.0 when >= LAYER3_MIN_SIGNALS of A/D/E/F fire.
options_partial is always set True on the incoming bundle.
"""

import logging

from backend import config

logger = logging.getLogger(__name__)


def score(data_bundle: dict) -> dict:
    """
    Layer 3 scorer.

    data_bundle keys:
        ticker           str
        options_signals  dict   -- from fetch_options_signals()
        last_price       float | None  -- today's closing price
        vwap             float | None  -- from fetch_vwap()

    Always mutates data_bundle["options_partial"] = True.

    Returns:
        {
          "score":   float  (0.0 or 1.0),
          "signals": {
              "A": bool,   call_vol_oi_ratio > tiered threshold
                           (0.5 mid / 1.5 large / 3.0 mega, by avg_daily_option_volume)
              "B": False,  option sweeps -- paid data only
              "C": False,  dark-pool prints -- paid data only
              "D": bool,   iv_slope < 0 (front IV < back IV, normal contango)
              "E": bool,   pc_ratio < pc_ratio_5d_avg (relative call demand)
              "F": bool,   last_price > vwap
              ... raw metric fields ...
          }
        }
    """
    data_bundle["options_partial"] = True

    ticker   = data_bundle.get("ticker", "?")
    opts     = data_bundle.get("options_signals", {})
    last_px  = data_bundle.get("last_price")
    vwap     = data_bundle.get("vwap")

    opts_populated = bool(opts) and any(v is not None for v in opts.values())
    logger.info(
        "L3 [%s] entering scorer | opts=%s | last_price=%s  vwap=%s",
        ticker,
        "POPULATED" if opts_populated else "EMPTY/ALL-NONE",
        f"{last_px:.2f}" if last_px is not None else "None",
        f"{vwap:.2f}" if vwap is not None else "None",
    )

    # -- A: aggressive call opening (high vol relative to standing OI) --------
    cvor = opts.get("call_vol_oi_ratio")
    adov = opts.get("avg_daily_option_volume") or 0

    if adov > config.L3_SIG_A_VOL_CUTOFF_HIGH:
        cvor_threshold = config.L3_SIG_A_RATIO_MEGA
        tier = "mega"
    elif adov > config.L3_SIG_A_VOL_CUTOFF_LOW:
        cvor_threshold = config.L3_SIG_A_RATIO_LARGE
        tier = "large"
    else:
        cvor_threshold = config.L3_SIG_A_RATIO_MID
        tier = "mid"

    sig_A = bool(cvor is not None and cvor > cvor_threshold)
    if cvor is None:
        logger.info("L3 [%s] A=False  call_vol_oi_ratio=None (no data)  [%s tier, adov=%d]",
                    ticker, tier, adov)
    else:
        logger.info("L3 [%s] A=%s  call_vol_oi_ratio=%.4f vs threshold=%.2f  [%s tier, adov=%d]",
                    ticker, sig_A, cvor, cvor_threshold, tier, adov)

    # -- B / C: stubs (paid feed) ---------------------------------------------
    sig_B = False
    sig_C = False

    # -- D: IV term structure in contango (front < back) ----------------------
    iv_slope = opts.get("iv_slope")
    sig_D    = bool(iv_slope is not None and iv_slope < 0)
    if iv_slope is None:
        logger.info("L3 [%s] D=False  iv_slope=None (no data)", ticker)
    else:
        logger.info("L3 [%s] D=%s  iv_slope=%.6f (need < 0 for contango)", ticker, sig_D, iv_slope)

    # -- E: today's put/call ratio below its 5-session rolling average --------
    pcr     = opts.get("put_call_ratio")
    pcr_avg = opts.get("pc_ratio_5d_avg")
    sig_E   = bool(pcr is not None and pcr_avg is not None and pcr < pcr_avg)
    if pcr is None:
        logger.info("L3 [%s] E=False  put_call_ratio=None (no data)", ticker)
    elif pcr_avg is None:
        logger.info("L3 [%s] E=False  put_call_ratio=%.4f  pc_ratio_5d_avg=None "
                    "(< 5 historical rows in DB)", ticker, pcr)
    else:
        logger.info("L3 [%s] E=%s  put_call_ratio=%.4f vs 5d_avg=%.4f (need pcr < avg)",
                    ticker, sig_E, pcr, pcr_avg)

    # -- F: last price above intraday VWAP ------------------------------------
    if last_px is not None and vwap is not None and vwap > 0:
        sig_F = bool(last_px > vwap)
        logger.info("L3 [%s] F=%s  last_price=%.2f vs vwap=%.2f",
                    ticker, sig_F, last_px, vwap)
    else:
        sig_F = False
        logger.info("L3 [%s] F=False  last_price=%s  vwap=%s (missing/zero)",
                    ticker,
                    f"{last_px:.2f}" if last_px is not None else "None",
                    f"{vwap:.2f}" if vwap is not None else "None")

    fired = int(sig_A) + int(sig_D) + int(sig_E) + int(sig_F)
    final_score = 1.0 if fired >= config.LAYER3_MIN_SIGNALS else 0.0

    logger.info(
        "L3 [%s] RESULT: A=%s D=%s E=%s F=%s  fired=%d/%d  score=%.1f",
        ticker, sig_A, sig_D, sig_E, sig_F, fired, config.LAYER3_MIN_SIGNALS, final_score,
    )

    return {
        "score": final_score,
        "signals": {
            "A": sig_A,
            "B": sig_B,
            "C": sig_C,
            "D": sig_D,
            "E": sig_E,
            "F": sig_F,
            "call_vol_oi_ratio":      round(cvor, 3)     if cvor     is not None else None,
            "cvor_threshold":         cvor_threshold,
            "tier":                   tier,
            "avg_daily_option_volume": adov,
            "iv_slope":               round(iv_slope, 4) if iv_slope is not None else None,
            "pc_ratio":               round(pcr, 3)      if pcr      is not None else None,
            "pc_ratio_5d_avg":        round(pcr_avg, 3)  if pcr_avg  is not None else None,
            "vwap":                   round(vwap, 2)      if vwap     is not None else None,
            "last_price":             round(last_px, 2)   if last_px  is not None else None,
            "fired":                  fired,
        },
    }
