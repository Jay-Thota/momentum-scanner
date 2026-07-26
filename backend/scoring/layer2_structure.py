"""
Layer 2: Price Structure & Momentum
Max 2.0 pts -- 1.0 from archetype match (first wins), 1.0 from 2B momentum checks.
"""

import logging

import pandas as pd
import pandas_ta as ta

from backend import config

logger = logging.getLogger(__name__)

_MIN_ROWS = 200  # need 200 for SMA-200; Alpaca IEX returns ~206 rows for 300-day lookback


# ---------------------------------------------------------------------------
# Indicator computation
# ---------------------------------------------------------------------------

def _add_indicators(ohlcv: pd.DataFrame) -> pd.DataFrame:
    df = ohlcv.copy()
    df["ma20"]  = ta.sma(df["Close"], length=20)
    df["ma50"]  = ta.sma(df["Close"], length=50)
    df["ma200"] = ta.sma(df["Close"], length=200)
    df["roc10"] = ta.roc(df["Close"], length=config.ROC_PERIOD)

    adx_df = ta.adx(df["High"], df["Low"], df["Close"], length=14)
    df["adx"]  = adx_df["ADX_14"]
    df["di_p"] = adx_df["DMP_14"]   # +DI
    df["di_n"] = adx_df["DMN_14"]   # -DI
    return df


# ---------------------------------------------------------------------------
# Archetype detectors
# ---------------------------------------------------------------------------

def _arch1_uptrend(df: pd.DataFrame) -> bool:
    """Price > 50MA AND > 200MA, both MAs sloping up over last MA_SLOPE_LOOKBACK sessions."""
    row = df.iloc[-1]
    ma50, ma200 = row["ma50"], row["ma200"]
    if pd.isna(ma50) or pd.isna(ma200):
        return False
    if row["Close"] <= ma50 or row["Close"] <= ma200:
        return False
    lb = config.MA_SLOPE_LOOKBACK
    if len(df) <= lb:
        return False
    ma50_up  = df["ma50"].iloc[-1]  > df["ma50"].iloc[-1 - lb]
    ma200_up = df["ma200"].iloc[-1] > df["ma200"].iloc[-1 - lb]
    return bool(ma50_up and ma200_up)


def _arch2_early_recovery(df: pd.DataFrame) -> bool:
    """Golden cross (50MA crosses above 200MA) in the last GOLDEN_CROSS_LOOKBACK sessions."""
    lb = config.GOLDEN_CROSS_LOOKBACK
    if len(df) < lb + 2:
        return False
    ma50, ma200 = df["ma50"], df["ma200"]
    if pd.isna(ma50.iloc[-1]) or pd.isna(ma200.iloc[-1]):
        return False
    cross_now  = ma50.iloc[-1]       > ma200.iloc[-1]
    cross_then = ma50.iloc[-lb - 1]  < ma200.iloc[-lb - 1]
    return bool(cross_now and cross_then)


def _arch3_breakout(df: pd.DataFrame, avg_vol_20d: float) -> tuple[bool, dict]:
    """
    Breakout: a session in the last 20 days closed >= BREAKOUT_PCT_MIN above prior close,
    on >= BREAKOUT_VOLUME_RATIO x average volume, AND the BREAKOUT_HOLD_SESSIONS sessions
    after the breakout day all closed within 2 pct of the breakout close.
    """
    pct_min    = config.BREAKOUT_PCT_MIN
    vol_min    = config.BREAKOUT_VOLUME_RATIO * avg_vol_20d
    hold_n     = config.BREAKOUT_HOLD_SESSIONS
    buf        = 0.02         # 2 pct tolerance for the hold check
    scan_back  = 20

    # We need hold_n confirmation rows after a candidate, so scan up to row -(hold_n+1)
    end_idx = len(df) - hold_n - 1
    start_idx = max(1, len(df) - scan_back - hold_n)

    for i in range(end_idx, start_idx - 1, -1):
        c_today = df["Close"].iloc[i]
        c_prev  = df["Close"].iloc[i - 1]
        vol     = df["Volume"].iloc[i]

        pct = (c_today - c_prev) / c_prev if c_prev > 0 else 0.0
        if pct < pct_min or vol < vol_min:
            continue

        # Confirm hold_n sessions after breakout day
        held = all(
            df["Close"].iloc[i + j] >= c_today * (1 - buf)
            for j in range(1, hold_n + 1)
        )
        if held:
            return True, {
                "breakout_date": str(df.index[i])[:10],
                "pct_chg":       round(pct, 3),
                "vol_ratio":     round(vol / avg_vol_20d, 2),
            }

    return False, {}


def _arch4_sector_rotation(
    df: pd.DataFrame,
    etf_df: pd.DataFrame,
    rs_rank: int | None,
) -> bool:
    """
    Sector rotation: sector ETF at or within 1 pct of its 20d high,
    AND stock is top-3 RS in its sector (rank <= 3, or unchecked if rank is None).
    """
    if etf_df.empty or len(etf_df) < 22:
        return False
    etf_close   = etf_df["Close"]
    etf_high_20 = etf_close.iloc[-20:].max()
    etf_ok      = etf_close.iloc[-1] >= etf_high_20 * 0.99
    if not etf_ok:
        return False
    if rs_rank is not None:
        return rs_rank <= 3
    return True   # ETF condition met; RS rank not provided


# ---------------------------------------------------------------------------
# 2B Momentum score
# ---------------------------------------------------------------------------

def _momentum_score(df: pd.DataFrame) -> tuple[float, dict]:
    """
    Signal A: ROC(10) > 0 AND ADX > ADX_MIN AND +DI > -DI  --> L2_MOMENTUM_A_SCORE pts
    Signal B: dist-day count < DISTRIBUTION_DAY_MAX AND close in upper range  --> L2_MOMENTUM_B_SCORE pts
    Signal C: MA bonus -- MA_BONUS_PER_LEVEL per MA level price is above (ma20/50/200)
    Cap total at 1.0.
    """
    row = df.iloc[-1]

    # -- Signal A --
    roc10 = row.get("roc10", float("nan"))
    adx   = row.get("adx",   float("nan"))
    di_p  = row.get("di_p",  float("nan"))
    di_n  = row.get("di_n",  float("nan"))

    if any(pd.isna(v) for v in [roc10, adx, di_p, di_n]):
        sig_A = False
    else:
        sig_A = bool(roc10 > 0 and adx > config.ADX_MIN and di_p > di_n)

    # -- Signal B --
    close_diff   = df["Close"].diff()
    prev_vol     = df["Volume"].shift(1)
    dist_mask    = (close_diff < 0) & (df["Volume"] > prev_vol)
    dist_count   = int(dist_mask.iloc[-20:].sum())

    hl_range = float(row["High"] - row["Low"])
    close_pct = (
        (float(row["Close"]) - float(row["Low"])) / hl_range
        if hl_range > 0 else 0.5
    )
    sig_B = bool(dist_count < config.DISTRIBUTION_DAY_MAX
                 and close_pct >= config.UPPER_RANGE_CLOSE_MIN)

    # -- Signal C: MA bonus --
    price  = float(row["Close"])
    ma_pts = 0.0
    ma_bonus_per = config.MA_BONUS_PER_LEVEL
    for ma_col in ("ma20", "ma50", "ma200"):
        val = row.get(ma_col, float("nan"))
        if not pd.isna(val) and price > val:
            ma_pts += ma_bonus_per

    raw = (
        (config.L2_MOMENTUM_A_SCORE if sig_A else 0.0)
        + (config.L2_MOMENTUM_B_SCORE if sig_B else 0.0)
        + ma_pts
    )
    mom_score = min(raw, 1.0)

    signals = {
        "A":          sig_A,
        "B":          sig_B,
        "C_pts":      round(ma_pts, 2),
        "roc10":      None if pd.isna(roc10) else round(float(roc10), 3),
        "adx":        None if pd.isna(adx)   else round(float(adx), 2),
        "di_p":       None if pd.isna(di_p)  else round(float(di_p), 2),
        "di_n":       None if pd.isna(di_n)  else round(float(di_n), 2),
        "dist_days":  dist_count,
        "close_pct":  round(close_pct, 3),
    }
    return mom_score, signals


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def score(data_bundle: dict) -> dict:
    """
    Layer 2: Price Structure & Momentum.

    data_bundle keys:
        ticker          -- str
        ohlcv           -- pd.DataFrame (from fetch_daily_ohlcv, includes OBV)
        sector_etf_ohlcv -- pd.DataFrame or empty (sector ETF daily OHLCV)
        sector_rs_rank  -- int | None  (rank within sector, 1 = strongest RS)

    Returns:
        {
          "archetype":      int | None,
          "archetype_score": float,
          "momentum_score":  float,
          "total":           float,
          "signals":         dict,
        }
    """
    _empty = {
        "archetype": None, "archetype_score": 0.0,
        "momentum_score": 0.0, "total": 0.0, "signals": {},
    }

    ticker   = data_bundle.get("ticker", "?")
    ohlcv    = data_bundle.get("ohlcv", pd.DataFrame())
    etf_ohlcv = data_bundle.get("sector_etf_ohlcv", pd.DataFrame())
    rs_rank   = data_bundle.get("sector_rs_rank")

    if ohlcv.empty or len(ohlcv) < _MIN_ROWS:
        logger.warning(
            "Layer 2: insufficient data for %s (%d rows, need %d)",
            ticker, len(ohlcv), _MIN_ROWS,
        )
        return _empty

    df = _add_indicators(ohlcv)

    avg_vol_20d = float(df["Volume"].iloc[-21:-1].mean())

    # ------------------------------------------------------------------
    # Archetype detection -- first match wins
    # ------------------------------------------------------------------
    archetype  = None
    arch_extra: dict = {}

    if _arch1_uptrend(df):
        archetype = 1
    elif _arch2_early_recovery(df):
        archetype = 2
    else:
        fired, arch_extra = _arch3_breakout(df, avg_vol_20d)
        if fired:
            archetype = 3
        elif _arch4_sector_rotation(df, etf_ohlcv, rs_rank):
            archetype = 4

    arch_score = 1.0 if archetype is not None else 0.0

    # ------------------------------------------------------------------
    # 2B Momentum
    # ------------------------------------------------------------------
    mom_score, mom_sigs = _momentum_score(df)

    total = arch_score + mom_score

    logger.debug(
        "%s arch=%s arch_score=%.1f mom=%.2f total=%.2f",
        ticker, archetype, arch_score, mom_score, total,
    )

    return {
        "archetype":       archetype,
        "archetype_score": arch_score,
        "momentum_score":  round(mom_score, 2),
        "total":           round(total, 2),
        "signals": {
            "arch_info":   arch_extra,
            **mom_sigs,
        },
    }
