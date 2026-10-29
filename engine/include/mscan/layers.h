#pragma once
// C++ ports of backend/scoring/layer1_volume.py and layer2_structure.py.
// These are the pure-OHLCV layers, which is why they are the only ones that
// move to C++: L0 market cap/earnings, L3 options and L4 short interest need
// network data and stay in Python.
//
// Result structs mirror the Python dicts field for field so the pybind11
// bindings can return identical dicts and the parity tests can compare 1:1.

#include <optional>
#include <string>

#include "mscan/universe.h"

namespace mscan {

// Mirrors backend/config.py defaults. Python passes its live values in, so env
// overrides still work and there is one source of truth.
struct ScoringConfig {
  // Layer 1
  int obv_lookback_days = 20;
  double volume_surge_ratio = 2.0;
  double rvol_threshold = 1.5;
  double up_down_vol_ratio_min = 1.5;
  int layer1_min_signals = 2;
  // Layer 2
  int adx_min = 20;
  int roc_period = 10;
  int ma_slope_lookback = 5;
  int golden_cross_lookback = 10;
  double breakout_pct_min = 0.08;
  double breakout_volume_ratio = 2.0;
  int breakout_hold_sessions = 2;
  int distribution_day_max = 3;
  double upper_range_close_min = 0.7;
  double ma_bonus_per_level = 0.17;
  double l2_momentum_a_score = 0.25;
  double l2_momentum_b_score = 0.25;
};

struct Layer1Result {
  double score = 0.0;          // 0.0 or 1.0
  bool A = false, B = false, C = false, D = false, E = false;
  double vol_ratio = 0.0;
  double up_down_ratio = 0.0;
  int block_trades = 0;
  int signals_fired = 0;
  bool valid = false;          // false == Python's "insufficient data" empty dict
};

struct Layer2Result {
  std::optional<int> archetype;   // 1..4, first match wins
  double archetype_score = 0.0;
  double momentum_score = 0.0;
  double total = 0.0;
  // momentum signals
  bool mom_A = false, mom_B = false;
  double ma_pts = 0.0;
  double roc10 = 0.0, adx = 0.0, di_p = 0.0, di_n = 0.0;
  int dist_days = 0;
  double close_pct = 0.0;
  // breakout details (archetype 3 only)
  int breakout_day = -1;
  double breakout_pct = 0.0, breakout_vol_ratio = 0.0;
  bool valid = false;
};

// Sector context for archetype 4. Empty optional == Python passing an empty
// ETF frame / None rank (which is what Step 4 of run_daily_scan does today).
struct SectorContext {
  BarSeries etf;
  std::optional<int> rs_rank;
};

Layer1Result score_layer1(const BarSeries& bars, int block_trades,
                          const ScoringConfig& cfg);

Layer2Result score_layer2(const BarSeries& bars,
                          const std::optional<SectorContext>& sector,
                          const ScoringConfig& cfg);

}  // namespace mscan
