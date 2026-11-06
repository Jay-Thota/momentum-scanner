#pragma once
// Event-driven, day-by-day replay of the price/volume part of the scanner.
//
// What it can and can't test (say this up front in the README):
//   - Historically reproducible from OHLCV: L0 price/volume gate, L1, L2,
//     regime filter (SPY vs 200MA), sector ETF trend + 63d RS rank (L5 parts).
//   - NOT reproducible on free data: L3 options flow, L4 short interest,
//     L0 market cap / earnings dates. The backtest scores a "PV-only" variant
//     with its own threshold. Be explicit that it validates part of the model.
//
// Bias controls:
//   - Look-ahead: on day d every scorer gets universe.series(i, d) and
//     streaming state fed bars up to d. Nothing else is reachable. Forward
//     returns are computed afterwards, in a separate pass. Entries fill at the
//     close of d (the scanner runs at 3 PM) or, more conservatively, at the
//     open of d+1 (FillPolicy).
//   - Survivorship: the universe file should include tickers that later
//     delisted. If it doesn't, the README must say results are optimistic.

#include <cstdint>
#include <string>
#include <vector>

#include "mscan/layers.h"
#include "mscan/thread_pool.h"
#include "mscan/universe.h"

namespace mscan {

enum class FillPolicy { kCloseSameDay, kOpenNextDay };

struct BacktestConfig {
  std::size_t start_day = 0;      // index into universe dates
  std::size_t end_day = 0;        // inclusive
  int top_n = 10;
  double min_pv_score = 2.0;      // threshold for the PV-only score (L1 + L2 max 3.0)
  std::vector<int> horizons = {5, 10, 20};   // forward-return windows, trading days
  FillPolicy fill = FillPolicy::kOpenNextDay;
  // Production raises the watchlist bar in a bear regime (WATCHLIST_MIN_SCORE
  // 4.0 -> BEAR_REGIME_MIN_SCORE 5.0); the PV-only score does the same with
  // its own threshold. Regime = regime_filter.get_regime on SPY instead of
  // ^GSPC: close above its 200-day MA and the MA's slope over
  // scoring.ma_slope_lookback sessions >= regime_slope_min.
  bool apply_regime_filter = true;
  double bear_min_pv_score = 2.5;
  double regime_slope_min = 0.0;  // SECTOR_SLOPE_MIN
  // Step 2's price/volume gate (MIN_PRICE, MIN_AVG_VOLUME over 20 bars)
  double min_price = 10.0;
  double min_avg_volume = 1'000'000.0;
  // eligible[i] != 0: ticker i can be picked. Empty: every ticker but SPY.
  // Use it to keep SPY and the sector ETFs out of the picks.
  std::vector<std::uint8_t> eligible;
  ScoringConfig scoring;
};

struct Pick {
  std::size_t day;                   // index into universe dates
  int32_t date;
  uint32_t ticker_idx;
  double score;
  std::vector<double> fwd_returns;   // one per horizon; NaN if past end of data
  std::vector<double> excess_vs_spy; // same, minus SPY over the same window
};

// Per-pick numbers use picks whose h-day return and excess are both known.
// sharpe and max_drawdown come from a staggered portfolio: each day's picks
// get 1/h of the capital, equal weight, for h days (daily rebalanced; a day
// with no picks leaves its sleeve in cash). That gives one return per
// trading day, so the Sharpe ratio is mean / stdev * sqrt(252), with no
// risk-free rate.
struct HorizonStats {
  int horizon;
  double hit_rate;          // fraction of picks with positive excess return
  double mean_return;
  double mean_excess;
  double sharpe;            // annualized, on the equal-weight daily pick portfolio
  double max_drawdown;      // largest peak-to-trough fall of that portfolio, as a fraction
  std::size_t n;
};

struct BacktestResult {
  std::vector<Pick> picks;
  std::vector<HorizonStats> stats;
  double elapsed_ms;
  std::size_t evaluations = 0;   // ticker-days scored
  std::size_t bear_days = 0;     // days the bear threshold applied
};

// spy_idx: index of SPY inside the universe (benchmark + regime filter).
// Scoring runs on `pool`, one ticker per task chunk; results do not depend on
// the number of threads. Throws std::invalid_argument on a bad config.
BacktestResult run_backtest(const Universe& u, std::size_t spy_idx,
                            const BacktestConfig& cfg, ThreadPool& pool);

}  // namespace mscan
