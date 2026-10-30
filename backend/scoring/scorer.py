"""
backend/scoring/scorer.py

Full scoring pipeline.  The fetch order is designed to minimise Yahoo Finance
API calls and avoid rate-limiting:

  Step 1  Alpaca batch OHLCV for all ~580 tickers (3 API calls total)
  Step 2  L0 price + volume filter        -- no Yahoo
  Step 3  Sector ETF pre-fetch            -- 11 Yahoo calls (ETFs only)
  Step 4  L1 + L2 on Alpaca OHLCV        -- no Yahoo
  Step 5  Early exit (L1=0 and L2<0.5)   -- no Yahoo
  Step 6  MetadataCache batch             -- Yahoo only for stale/missing (~0 after first run)
  Step 7  Complete L0 (market_cap + earnings gate)
  Step 8  FINRA short files               -- HTTP, disk-cached
  Step 9  Per-ticker: L4 + L5 + L3       -- Yahoo only for fundamentals + options

Public API:
    score_stock(ticker, data_bundle, regime) -> ScoringResult | None
    run_daily_scan(stats=None)              -> list[ScoringResult]
"""

import json
import logging
import time
from collections.abc import Mapping, MutableMapping
from concurrent.futures import ThreadPoolExecutor, as_completed, wait as futures_wait
from dataclasses import asdict, dataclass, field
from datetime import date, datetime

import pandas as pd
from sqlalchemy import select as _sa_select

from backend import config
from backend.data.fetcher_options import batch_upsert_options, fetch_options_signals, fetch_vwap
from backend.data.fetcher_price import (
    fetch_batch,
    fetch_daily_bars_alpaca,
    fetch_daily_ohlcv,
    fetch_daily_ohlcv_batch_alpaca,
)
from backend.data.ohlcv_batch import LazyOhlcvMap, OhlcvBatch
from backend.data.fetcher_sector import (
    SECTOR_PEERS,
    build_sector_returns,
    compute_sector_rs_rank,
    fetch_sector_data,
)
from backend.data.fetcher_short import download_finra_ssho, get_short_trend
from backend.db.database import SessionLocal, init_db
from backend.db.metadata_cache import get_metadata_batch
from backend.db.models import ScanResult as ScanResultRow, StockSignals
from backend.scoring import (
    layer0_universe as layer0,
    layer1_volume as layer1,
    layer2_structure as layer2,
    layer3_options as layer3,
    layer4_short as layer4,
    layer5_sector as layer5,
)
from backend.universe import get_universe
from backend.utils.regime_filter import get_regime

logger = logging.getLogger(__name__)

_L0_VOLUME_WINDOW = 20   # Step 2: bars required, and the average-volume window


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class ScoringResult:
    ticker:    str
    total:     float
    l1:        float
    l2:        float
    l3:        float
    l4:        float
    l5:        float
    archetype: str | None
    price:     float | None   = None
    signals:   dict           = field(default_factory=dict)
    timestamp: str            = field(default_factory=lambda: datetime.now().isoformat())
    min_score: float          = field(default=config.WATCHLIST_MIN_SCORE)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# DB persistence
# ---------------------------------------------------------------------------

def save_results_to_db(results: list["ScoringResult"], regime: str) -> None:
    """Persist watchlist results to scan_results + stock_signals tables."""
    init_db()
    today = date.today().isoformat()

    with SessionLocal() as session:
        for r in results:
            existing = session.execute(
                _sa_select(ScanResultRow)
                .where(ScanResultRow.scan_date == today)
                .where(ScanResultRow.ticker == r.ticker)
            ).scalar_one_or_none()
            if existing is not None:
                session.delete(existing)
                session.flush()

            row = ScanResultRow(
                scan_date   = today,
                ticker      = r.ticker,
                total_score = r.total,
                price       = r.price,
                l1          = r.l1,
                l2          = r.l2,
                l3          = r.l3,
                l4          = r.l4,
                l5          = r.l5,
                archetype   = str(r.archetype) if r.archetype is not None else None,
                regime      = regime,
                timestamp   = r.timestamp,
            )
            session.add(row)
            session.flush()

            for layer_key in ("l1", "l2", "l3", "l4", "l5"):
                session.add(StockSignals(
                    scan_result_id = row.id,
                    signal_key     = layer_key,
                    signal_value   = json.dumps(r.signals.get(layer_key), default=str),
                ))

        session.commit()

    logger.info("Saved %d results to DB (scan_date=%s)", len(results), today)


# ---------------------------------------------------------------------------
# Public: score a pre-fetched bundle (for testing / webhook)
# ---------------------------------------------------------------------------

def score_stock(
    ticker: str,
    data_bundle: dict,
    regime: dict,
) -> "ScoringResult | None":
    """Score a ticker from a pre-built data_bundle (all layer keys must be present)."""
    if not layer0.check(ticker, data_bundle):
        return None

    l4_score = layer4.score(data_bundle)
    if l4_score is None:
        return None

    l1_result = layer1.score(data_bundle)
    l2_result = layer2.score(data_bundle)
    l3_result = layer3.score(data_bundle)
    l5_result = layer5.score(data_bundle, regime)

    l2_score = l2_result.get("total", 0.0)
    total    = round(
        l1_result["score"] + l2_score + l3_result["score"] + l4_score + l5_result["score"],
        2,
    )

    min_score = (
        config.BEAR_REGIME_MIN_SCORE if regime.get("regime") == "bear"
        else config.WATCHLIST_MIN_SCORE
    )

    return ScoringResult(
        ticker    = ticker,
        total     = total,
        l1        = l1_result["score"],
        l2        = l2_score,
        l3        = l3_result["score"],
        l4        = l4_score,
        l5        = l5_result["score"],
        archetype = str(l2_result.get("archetype")) if l2_result.get("archetype") is not None else None,
        signals   = {
            "l1": l1_result["signals"],
            "l2": {"archetype": l2_result.get("archetype"), **l2_result.get("signals", {})},
            "l3": l3_result["signals"],
            "l4": {"si_pct": data_bundle.get("si_pct"), "days_to_cover": data_bundle.get("days_to_cover"),
                   "si_trend": data_bundle.get("si_trend"), "bonus": l4_score},
            "l5": l5_result["signals"],
        },
        timestamp = datetime.now().isoformat(),
        min_score = min_score,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def fetch_ohlcv_map(universe: list[str]) -> MutableMapping[str, pd.DataFrame]:
    """
    Step 1: Alpaca daily bars as a {ticker: DataFrame} mapping. With
    USE_CPP_ENGINE the bars go straight into (T, D) arrays for the engine and
    a DataFrame is only built when a later step asks for that ticker.
    """
    if config.USE_CPP_ENGINE:
        raw = fetch_daily_bars_alpaca(universe, raw=True)
        return LazyOhlcvMap(OhlcvBatch.from_raw_bars(raw, list(raw)))
    return fetch_daily_ohlcv_batch_alpaca(universe)


def _n_rows(ohlcv_map: Mapping[str, pd.DataFrame], ticker: str) -> int:
    if isinstance(ohlcv_map, LazyOhlcvMap):
        return ohlcv_map.n_rows(ticker)   # without building the DataFrame
    return len(ohlcv_map.get(ticker, pd.DataFrame()))


def l0_price_volume_pass(tickers: list[str], ohlcv_map: Mapping[str, pd.DataFrame]) -> list[str]:
    """Step 2: tickers with >= 20 bars, last close >= MIN_PRICE and 20-bar avg volume >= MIN_AVG_VOLUME."""
    if isinstance(ohlcv_map, LazyOhlcvMap):
        known = [t for t in tickers if t in ohlcv_map.batch.row]
        close, avg = ohlcv_map.batch.last_close_and_avg_volume(known, _L0_VOLUME_WINDOW)
        passed = {t for t, c, a in zip(known, close, avg)
                  if c >= config.MIN_PRICE and a >= config.MIN_AVG_VOLUME}
        return [t for t in tickers if t in passed]

    out: list[str] = []
    for ticker in tickers:
        df = ohlcv_map.get(ticker, pd.DataFrame())
        if df.empty or len(df) < _L0_VOLUME_WINDOW:
            continue
        price   = float(df["Close"].iloc[-1])
        avg_vol = float(df["Volume"].tail(_L0_VOLUME_WINDOW).mean())
        if price >= config.MIN_PRICE and avg_vol >= config.MIN_AVG_VOLUME:
            out.append(ticker)
    return out


def _score_l1l2_python(tickers: list[str], ohlcv_map: dict[str, pd.DataFrame]) -> dict[str, dict]:
    l1l2_map: dict[str, dict] = {}
    for ticker in tickers:
        df = ohlcv_map[ticker]
        l1 = layer1.score({"ticker": ticker, "ohlcv": df, "block_trades": 0})
        l2 = layer2.score({
            "ticker":           ticker,
            "ohlcv":            df,
            "sector_etf_ohlcv": pd.DataFrame(),  # sector unknown until Step 6
            "sector_rs_rank":   None,
        })
        l1l2_map[ticker] = {"l1": l1, "l2": l2}
    return l1l2_map


def score_l1l2(
    tickers: list[str], ohlcv_map: dict[str, pd.DataFrame], use_cpp: bool | None = None,
) -> dict[str, dict]:
    """
    Step 4: layer 1 + layer 2 for every ticker -> {ticker: {"l1": ..., "l2": ...}}.
    Runs on the C++ engine when use_cpp (default config.USE_CPP_ENGINE) is set,
    falling back to Python if the engine is not installed or raises.
    """
    if config.USE_CPP_ENGINE if use_cpp is None else use_cpp:
        try:
            from backend.scoring.cpp_engine import score_l1l2_cpp
            return score_l1l2_cpp(tickers, ohlcv_map)
        except Exception as exc:
            logger.warning("C++ engine unavailable or failed (%s: %s) -- using Python for Step 4",
                           type(exc).__name__, exc)
    return _score_l1l2_python(tickers, ohlcv_map)


def _prefetch_sector_data() -> tuple[dict, dict, dict]:
    """Pre-fetch ETF momentum stats, daily OHLCV, and peer returns for all 11 sectors."""
    all_etfs: list[str]              = list(SECTOR_PEERS.keys())
    etf_data:    dict[str, dict]     = {}
    etf_ohlcv:   dict[str, pd.DataFrame] = {}
    etf_returns: dict[str, dict]     = {}

    with ThreadPoolExecutor(max_workers=15) as ex:
        futs: dict = {}
        for etf in all_etfs:
            futs[ex.submit(fetch_sector_data,    etf)] = ("etf",  etf)
            futs[ex.submit(fetch_daily_ohlcv,    etf)] = ("ohlcv", etf)
            futs[ex.submit(build_sector_returns, etf)] = ("sr",   etf)
        for f in as_completed(futs):
            kind, etf = futs[f]
            try:
                r = f.result()
                if   kind == "etf":   etf_data[etf]    = r
                elif kind == "ohlcv": etf_ohlcv[etf]   = r
                else:                 etf_returns[etf] = r
            except Exception as exc:
                logger.warning("ETF pre-fetch %s/%s: %s", kind, etf, exc)

    return etf_data, etf_ohlcv, etf_returns


# ---------------------------------------------------------------------------
# Public: full daily scan
# ---------------------------------------------------------------------------

def run_daily_scan(stats: dict | None = None) -> list["ScoringResult"]:
    """
    Run the full daily scan using a rate-limit-aware fetch order.

    Yahoo calls are minimised by:
    - Using Alpaca batch OHLCV instead of per-ticker Yahoo history calls
    - Gating all per-ticker Yahoo calls behind L0 + L1/L2 early-exit filters
    - Caching slow-changing metadata (market_cap, sector, SI%) for 7 days

    If stats is provided it is populated with counters for each pipeline stage.
    """
    t_start = time.perf_counter()

    with SessionLocal() as _univ_db:
        universe = get_universe(_univ_db)
    logger.info("Stage 1 (universe): %d tickers loaded from cache", len(universe))

    regime    = get_regime()
    min_score = (
        config.BEAR_REGIME_MIN_SCORE if regime["regime"] == "bear"
        else config.WATCHLIST_MIN_SCORE
    )
    logger.info("Regime: %s  min_score=%.1f", regime["regime"], min_score)

    # ── Step 1: Alpaca batch OHLCV ─────────────────────────────────────────
    logger.info("Step 1: Alpaca batch OHLCV for %d tickers ...", len(universe))
    ohlcv_map = fetch_ohlcv_map(universe)

    alpaca_hit    = [t for t in universe if _n_rows(ohlcv_map, t) > 0]
    yahoo_missing = [t for t in universe if t not in alpaca_hit]

    logger.info(
        "  Alpaca: %d/%d tickers  |  Yahoo fallback needed: %d",
        len(alpaca_hit), len(universe), len(yahoo_missing),
    )

    if yahoo_missing:
        logger.info("  Fetching Yahoo OHLCV for %d missing tickers (5 workers) ...", len(yahoo_missing))
        fallback = fetch_batch(yahoo_missing, workers=5)
        ohlcv_map.update(fallback)

    total_with_data = sum(1 for t in ohlcv_map if _n_rows(ohlcv_map, t) > 0)
    logger.info("  Total tickers with OHLCV: %d", total_with_data)

    # ── Step 2: L0 price + volume (no Yahoo) ───────────────────────────────
    l0_pv_pass = l0_price_volume_pass(universe, ohlcv_map)
    logger.info("Step 2: L0 price+vol pass: %d/%d", len(l0_pv_pass), len(universe))

    # ── Step 3: Sector ETF pre-fetch (11 Yahoo ETF calls, in parallel) ─────
    logger.info("Step 3: Pre-fetching %d sector ETFs ...", len(SECTOR_PEERS))
    sector_etf_data, sector_etf_ohlcv, sector_returns = _prefetch_sector_data()

    # ── Step 4: L1 + L2 on OHLCV only (no Yahoo) ───────────────────────────
    logger.info("Step 4: L1+L2 scoring on %d tickers ...", len(l0_pv_pass))
    l1l2_map = score_l1l2(l0_pv_pass, ohlcv_map)

    # ── Step 5: Early exit ─────────────────────────────────────────────────
    survivors: list[str] = [
        t for t in l0_pv_pass
        if not (
            l1l2_map[t]["l1"]["score"] == 0.0
            and l1l2_map[t]["l2"].get("total", 0.0) < 0.5
        )
    ]
    discarded = len(l0_pv_pass) - len(survivors)
    logger.info(
        "Step 5: Early exit -- discarded %d  (L1=0 AND L2<0.5)  survivors: %d",
        discarded, len(survivors),
    )

    # ── Step 6: MetadataCache batch (Yahoo only for stale/missing) ──────────
    logger.info("Step 6: MetadataCache for %d survivors ...", len(survivors))
    meta_stats: dict = {}
    with SessionLocal() as session:
        metadata = get_metadata_batch(survivors, session, stats=meta_stats)

    # ── Step 7: Complete L0 (market_cap + earnings gate) ────────────────────
    l0_full_pass: list[str] = []
    for ticker in survivors:
        meta  = metadata.get(ticker, {})
        df    = ohlcv_map[ticker]
        price   = float(df["Close"].iloc[-1])
        avg_vol = float(df["Volume"].tail(20).mean())

        earnings_date: date | None = None
        ed_str = meta.get("earnings_next_date")
        if ed_str:
            try:
                earnings_date = date.fromisoformat(ed_str)
            except ValueError:
                pass

        if layer0.check(ticker, {
            "price":         price,
            "avg_volume":    avg_vol,
            "market_cap":    meta.get("market_cap", 0.0),
            "earnings_date": earnings_date,
            "ohlcv_rows":    len(df),
        }):
            l0_full_pass.append(ticker)

    logger.info(
        "Stage 2 (L0 gate): %d tickers pass price+vol+mcap+history+earnings",
        len(l0_full_pass),
    )

    # ── Step 8: FINRA short files (HTTP, disk-cached) ───────────────────────
    logger.info("Step 8: Downloading FINRA daily short sale files ...")
    current_finra = download_finra_ssho(offset_days=0)
    prev_finra    = download_finra_ssho(offset_days=10)

    # ── Step 9: Per-ticker final scoring ──────────────────────────────────────
    # Phase A: L4 gate (fast, cache-only) -- identify survivors
    # Phase B: Parallel L3 options + VWAP fetch for L4 survivors (20 workers)
    # Phase C: Batch OptionsHistory upsert (single session, after parallel fetch)
    # Phase D: Full scoring loop (L1+L2+L3+L4+L5) using pre-fetched data
    logger.info("Step 9: Final scoring for %d tickers ...", len(l0_full_pass))

    l4_fail_count = 0
    l4_cache_hits = 0
    l4_scores:    dict[str, float] = {}
    l4_si_trends: dict[str, str]   = {}

    # -- Phase A: L4 gate -----------------------------------------------------
    for ticker in l0_full_pass:
        meta     = metadata.get(ticker, {})
        si_trend = get_short_trend(ticker, current_finra, prev_finra)
        l4_cache_hits += 1
        l4_score = layer4.score({
            "ticker":         ticker,
            "si_pct":         meta.get("si_pct"),
            "days_to_cover":  meta.get("days_to_cover"),
            "si_trend":       si_trend,
            "eps_growth":     meta.get("eps_growth"),
            "revenue_growth": meta.get("revenue_growth"),
        })
        if l4_score is None:
            l4_fail_count += 1
        else:
            l4_scores[ticker]    = l4_score
            l4_si_trends[ticker] = si_trend

    l4_survivors = list(l4_scores)
    l3_fetched   = len(l4_survivors)
    logger.info(
        "Step 9 Phase A: L4 gate -- %d pass  %d fail",
        len(l4_survivors), l4_fail_count,
    )

    # -- Phase B: Parallel L3 options + VWAP fetch ----------------------------
    options_data: dict[str, dict]        = {}
    vwap_data:    dict[str, float | None] = {}

    def _fetch_l3_pair(t: str) -> tuple[dict, float | None]:
        return fetch_options_signals(t), fetch_vwap(t)

    # Budget: 3s per task / 20 workers + 10s buffer, minimum 30s
    _L3_BUDGET = max(30.0, len(l4_survivors) * 3.0 / 20 + 10)
    logger.info(
        "Step 9 Phase B: Parallel L3 fetch for %d tickers (20 workers, %.0fs budget) ...",
        len(l4_survivors), _L3_BUDGET,
    )
    _t_l3 = time.perf_counter()

    _executor = ThreadPoolExecutor(max_workers=20)
    _fmap = {_executor.submit(_fetch_l3_pair, t): t for t in l4_survivors}
    _done, _pending = futures_wait(_fmap, timeout=_L3_BUDGET)
    _executor.shutdown(wait=False)  # release pool; don't block on stragglers

    for _f in _done:
        _t = _fmap[_f]
        try:
            _opts, _vwap = _f.result()
        except Exception as _exc:
            logger.debug("L3 fetch error %s: %s", _t, _exc)
            _opts, _vwap = {}, None
        options_data[_t] = _opts
        vwap_data[_t]    = _vwap

    if _pending:
        logger.warning(
            "L3 fetch: %d tickers timed out after %.0fs budget -- using empty results",
            len(_pending), _L3_BUDGET,
        )
        for _f in _pending:
            _f.cancel()
            options_data[_fmap[_f]] = {}
            vwap_data[_fmap[_f]]    = None

    logger.info(
        "Step 9 Phase B: L3 done -- %d ok  %d timeout  elapsed=%.1fs",
        len(_done), len(_pending), time.perf_counter() - _t_l3,
    )

    # -- Phase C: Batch OptionsHistory upsert (single session) ----------------
    _today = date.today().isoformat()
    _opts_rows = [
        {
            "ticker":         t,
            "scan_date":      _today,
            "put_call_ratio": options_data[t].get("put_call_ratio"),
            "cvor":           options_data[t].get("call_vol_oi_ratio"),
            "iv_slope":       options_data[t].get("iv_slope"),
        }
        for t in l4_survivors
        if any(
            options_data[t].get(k) is not None
            for k in ("put_call_ratio", "call_vol_oi_ratio", "iv_slope")
        )
    ]
    try:
        batch_upsert_options(_opts_rows)
        logger.info("Step 9 Phase C: OptionsHistory batch upsert -- %d rows", len(_opts_rows))
    except Exception as _exc:
        logger.warning("OptionsHistory batch upsert failed: %s", _exc)

    # -- Phase D: Scoring loop (no fetches -- all data pre-loaded) ------------
    all_results: list[ScoringResult] = []
    for ticker in l4_survivors:
        try:
            meta       = metadata.get(ticker, {})
            df         = ohlcv_map[ticker]
            price      = float(df["Close"].iloc[-1])
            si_trend   = l4_si_trends[ticker]
            sector_etf = meta.get("etf_symbol")

            peers_ret = sector_returns.get(sector_etf, {}) if sector_etf else {}
            rs_rank   = compute_sector_rs_rank(ticker, peers_ret)
            etf_data  = sector_etf_data.get(sector_etf) if sector_etf else None
            etf_ohlcv = sector_etf_ohlcv.get(sector_etf, pd.DataFrame()) if sector_etf else pd.DataFrame()

            l4_score = l4_scores[ticker]

            l2_final  = layer2.score({
                "ticker":           ticker,
                "ohlcv":            df,
                "sector_etf_ohlcv": etf_ohlcv,
                "sector_rs_rank":   rs_rank,
            })
            l2_score  = l2_final.get("total", 0.0)
            archetype = l2_final.get("archetype")

            l1_result = l1l2_map[ticker]["l1"]

            opts      = options_data.get(ticker, {})
            vwap      = vwap_data.get(ticker)
            l3_result = layer3.score({
                "ticker":          ticker,
                "options_signals": opts,
                "last_price":      price,
                "vwap":            vwap,
            })

            l5_result = layer5.score({
                "ticker":     ticker,
                "sector_etf": sector_etf,
                "etf_data":   etf_data,
                "rs_rank":    rs_rank,
            }, regime)

            total = round(
                l1_result["score"] + l2_score + l3_result["score"] + l4_score + l5_result["score"],
                2,
            )

            all_results.append(ScoringResult(
                ticker    = ticker,
                total     = total,
                price     = round(price, 2),
                l1        = l1_result["score"],
                l2        = l2_score,
                l3        = l3_result["score"],
                l4        = l4_score,
                l5        = l5_result["score"],
                archetype = str(archetype) if archetype is not None else None,
                signals   = {
                    "l1": l1_result["signals"],
                    "l2": {
                        "archetype":  archetype,
                        "arch_score": l2_final.get("archetype_score"),
                        "momentum":   l2_final.get("momentum_score"),
                        **l2_final.get("signals", {}),
                    },
                    "l3": l3_result["signals"],
                    "l4": {
                        "si_pct":        meta.get("si_pct"),
                        "days_to_cover": meta.get("days_to_cover"),
                        "si_trend":      si_trend,
                        "bonus":         l4_score,
                    },
                    "l5": l5_result["signals"],
                },
                timestamp = datetime.now().isoformat(),
                min_score = min_score,
            ))

        except Exception as exc:
            logger.warning("Scoring error for %s: %s", ticker, exc, exc_info=False)

    # ── Filter, rank, persist ───────────────────────────────────────────────
    qualified = [r for r in all_results if r.total >= min_score]
    watchlist  = sorted(qualified, key=lambda r: r.total, reverse=True)[:10]

    elapsed = time.perf_counter() - t_start
    logger.info(
        "Scan complete -- universe=%d  alpaca=%d  yahoo_ohlcv=%d"
        "  l0_pv=%d  early_exit=%d  survivors=%d"
        "  meta_hits=%d  meta_yahoo=%d"
        "  l0_full=%d  l4_fail=%d  l4_cache=%d  l3_fetched=%d"
        "  scored=%d  qualified=%d  watchlist=%d  elapsed=%.1fs",
        len(universe), len(alpaca_hit), len(yahoo_missing),
        len(l0_pv_pass), discarded, len(survivors),
        meta_stats.get("metadata_cache_hits", 0), meta_stats.get("metadata_yahoo_calls", 0),
        len(l0_full_pass), l4_fail_count, l4_cache_hits, l3_fetched,
        len(all_results), len(qualified), len(watchlist), elapsed,
    )

    if stats is not None:
        stats.update({
            "universe_size":        len(universe),
            "alpaca_ohlcv_count":   len(alpaca_hit),
            "yahoo_ohlcv_fallback": len(yahoo_missing),
            "l0_pv_pass":           len(l0_pv_pass),
            "early_exit_discarded": discarded,
            "metadata_cache_hits":  meta_stats.get("metadata_cache_hits", 0),
            "metadata_yahoo_calls": meta_stats.get("metadata_yahoo_calls", 0),
            "l0_pass":              len(l0_full_pass),
            "l4_fail":              l4_fail_count,
            "l4_cache_hits":        l4_cache_hits,
            "l3_fetched":           l3_fetched,
            "scored":               len(all_results),
            "qualified":            len(qualified),
            "watchlist":            len(watchlist),
            "elapsed_s":            round(elapsed, 1),
            "regime":               regime["regime"],
            "min_score":            min_score,
        })

    try:
        save_results_to_db(watchlist, regime["regime"])
    except Exception as exc:
        logger.warning("save_results_to_db failed: %s", exc)

    return watchlist
