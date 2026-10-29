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
//   - Look-ahead: on day d every scorer gets universe.series(i, d). Nothing
//     else is reachable. Entries fill at close of d (scanner runs 3:30 PM, so
//     optionally at open of d+1 to be conservative; make it a flag).
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
  bool apply_regime_filter = true;
  ScoringConfig scoring;
};

struct Pick {
  int32_t date;
  uint32_t ticker_idx;
  double score;
  std::vector<double> fwd_returns;   // one per horizon; NaN if past end of data
  std::vector<double> excess_vs_spy; // same, minus SPY over the same window
};

struct HorizonStats {
  int horizon;
  double hit_rate;          // fraction of picks with positive excess return
  double mean_return;
  double mean_excess;
  double sharpe;            // annualized, on the equal-weight daily pick portfolio
  double max_drawdown;
  std::size_t n;
};

struct BacktestResult {
  std::vector<Pick> picks;
  std::vector<HorizonStats> stats;
  double elapsed_ms;
};

// spy_idx: index of SPY inside the universe (benchmark + regime filter).
BacktestResult run_backtest(const Universe& u, std::size_t spy_idx,
                            const BacktestConfig& cfg, ThreadPool& pool);

}  // namespace mscan
