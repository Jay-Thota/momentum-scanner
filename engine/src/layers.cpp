// Ports of backend/scoring/layer1_volume.py and layer2_structure.py.
//
// Every branch mirrors the Python, including the parts that look odd: NaN
// comparisons are false in both languages, so a NaN flows through the same
// branches here as it does there. Reported values are rounded the way Python
// rounds them (py_round for Python floats, np_round for numpy.float64).
//
// Unlike the indicator kernels, these functions allocate: they compute full
// indicator series into local vectors, exactly as layer2._add_indicators does.

#include "mscan/layers.h"

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <limits>
#include <span>
#include <vector>

#include "mscan/indicators.h"
#include "mscan/pyround.h"

namespace mscan {
namespace {

constexpr double kNaN = std::numeric_limits<double>::quiet_NaN();

// Constants hard-coded in layer2_structure.py rather than read from config.py
constexpr std::size_t kL2MinRows = 200;      // _MIN_ROWS
constexpr int kAdxPeriod = 14;               // _add_indicators: _compute_adx(period=14)
constexpr double kBreakoutHoldBuffer = 0.02; // _arch3_breakout: buf
constexpr std::ptrdiff_t kBreakoutScanBack = 20;
constexpr std::size_t kEtfMinRows = 22;      // _arch4_sector_rotation
constexpr std::size_t kEtfHighWindow = 20;
constexpr double kEtfNearHigh = 0.99;
constexpr int kTopRsRank = 3;
constexpr std::size_t kDistributionWindow = 20;

// pandas Series.sum() / .mean() / .max() all skip NaN.
//
// Summation order: pandas sums with numpy's pairwise algorithm and the loops
// here are sequential. They agree exactly whenever every partial sum is exact,
// which holds for what is summed here: volume (integer share counts, totals
// far below 2^53).
double nan_mean(std::span<const double> x) {
  double s = 0.0;
  std::size_t count = 0;
  for (double v : x) {
    if (!std::isnan(v)) {
      s += v;
      ++count;
    }
  }
  return count == 0 ? kNaN : s / static_cast<double>(count);
}

double nan_max(std::span<const double> x) {
  double m = kNaN;
  for (double v : x) {
    if (!std::isnan(v) && (std::isnan(m) || v > m)) m = v;
  }
  return m;
}

// x.iloc[-k:] (the whole series when it is shorter than k)
std::span<const double> last(std::span<const double> x, std::size_t k) {
  return x.subspan(x.size() - std::min(k, x.size()));
}

struct Indicators {
  std::vector<double> ma20, ma50, ma200, roc, adx, di_p, di_n;
};

// layer2._add_indicators
Indicators add_indicators(const BarSeries& bars, const ScoringConfig& cfg) {
  const std::size_t n = bars.size();
  Indicators ind{std::vector<double>(n), std::vector<double>(n), std::vector<double>(n),
                 std::vector<double>(n), std::vector<double>(n), std::vector<double>(n),
                 std::vector<double>(n)};
  ind::sma(bars.close, 20, ind.ma20);
  ind::sma(bars.close, 50, ind.ma50);
  ind::sma(bars.close, 200, ind.ma200);
  ind::roc(bars.close, cfg.roc_period, ind.roc);
  ind::adx(bars, kAdxPeriod, {ind.adx, ind.di_p, ind.di_n});
  return ind;
}

// _arch1_uptrend
bool arch1_uptrend(const BarSeries& bars, const Indicators& ind, const ScoringConfig& cfg) {
  const std::size_t n = bars.size();
  const double ma50 = ind.ma50[n - 1], ma200 = ind.ma200[n - 1], close = bars.close[n - 1];
  if (std::isnan(ma50) || std::isnan(ma200)) return false;
  if (close <= ma50 || close <= ma200) return false;   // a NaN close passes, as in Python
  const auto lb = static_cast<std::size_t>(cfg.ma_slope_lookback);
  if (n <= lb) return false;
  return ind.ma50[n - 1] > ind.ma50[n - 1 - lb] && ind.ma200[n - 1] > ind.ma200[n - 1 - lb];
}

// _arch2_early_recovery
bool arch2_golden_cross(const BarSeries& bars, const Indicators& ind, const ScoringConfig& cfg) {
  const std::size_t n = bars.size();
  const auto lb = static_cast<std::size_t>(cfg.golden_cross_lookback);
  if (n < lb + 2) return false;
  if (std::isnan(ind.ma50[n - 1]) || std::isnan(ind.ma200[n - 1])) return false;
  const bool cross_now = ind.ma50[n - 1] > ind.ma200[n - 1];
  const bool cross_then = ind.ma50[n - 1 - lb] < ind.ma200[n - 1 - lb];
  return cross_now && cross_then;
}

// _arch3_breakout: newest qualifying day first, within the last 20 sessions
// that still have breakout_hold_sessions bars after them.
bool arch3_breakout(const BarSeries& bars, double avg_vol_20d, const ScoringConfig& cfg,
                    Layer2Result& r) {
  const auto n = static_cast<std::ptrdiff_t>(bars.size());
  const auto& c = bars.close;
  const double vol_min = cfg.breakout_volume_ratio * avg_vol_20d;
  const std::ptrdiff_t hold = cfg.breakout_hold_sessions;
  const std::ptrdiff_t end_idx = n - hold - 1;
  const std::ptrdiff_t start_idx = std::max<std::ptrdiff_t>(1, n - kBreakoutScanBack - hold);

  for (std::ptrdiff_t i = end_idx; i >= start_idx; --i) {
    const double c_today = c[i], c_prev = c[i - 1], vol = bars.volume[i];
    const double pct = c_prev > 0 ? (c_today - c_prev) / c_prev : 0.0;
    // NaN pct or vol compares false here, so it does NOT skip the day (Python
    // does the same); a NaN c_today then fails every hold check below.
    if (pct < cfg.breakout_pct_min || vol < vol_min) continue;

    bool held = true;
    for (std::ptrdiff_t j = 1; j <= hold; ++j) {
      if (!(c[i + j] >= c_today * (1.0 - kBreakoutHoldBuffer))) {
        held = false;
        break;
      }
    }
    if (held) {
      r.breakout_day = static_cast<int>(i);
      r.breakout_pct = np_round(pct, 3);                    // numpy.float64 in Python
      r.breakout_vol_ratio = np_round(vol / avg_vol_20d, 2);
      return true;
    }
  }
  return false;
}

// _arch4_sector_rotation
bool arch4_sector_rotation(const std::optional<SectorContext>& sector) {
  if (!sector || sector->etf.size() < kEtfMinRows) return false;
  const auto& etf_close = sector->etf.close;
  const double high_20 = nan_max(last(etf_close, kEtfHighWindow));
  if (!(etf_close.back() >= high_20 * kEtfNearHigh)) return false;
  if (sector->rs_rank) return *sector->rs_rank <= kTopRsRank;
  return true;   // ETF condition met; RS rank not provided
}

// _momentum_score
double momentum(const BarSeries& bars, const Indicators& ind, const ScoringConfig& cfg,
                Layer2Result& r) {
  const std::size_t n = bars.size();
  const double roc10 = ind.roc[n - 1], adx = ind.adx[n - 1];
  const double di_p = ind.di_p[n - 1], di_n = ind.di_n[n - 1];

  // Signal A
  const bool any_nan = std::isnan(roc10) || std::isnan(adx) || std::isnan(di_p) || std::isnan(di_n);
  r.mom_A = !any_nan && roc10 > 0 && adx > cfg.adx_min && di_p > di_n;

  // Signal B: distribution days in the last 20 bars + close in the upper range
  int dist = 0;
  for (std::size_t i = n - std::min(kDistributionWindow, n); i < n; ++i) {
    if (i == 0) continue;   // diff() and shift(1) are NaN on the first bar
    if (bars.close[i] - bars.close[i - 1] < 0 && bars.volume[i] > bars.volume[i - 1]) ++dist;
  }
  const double hl_range = bars.high[n - 1] - bars.low[n - 1];
  const double close_pct = hl_range > 0 ? (bars.close[n - 1] - bars.low[n - 1]) / hl_range : 0.5;
  r.mom_B = dist < cfg.distribution_day_max && close_pct >= cfg.upper_range_close_min;

  // Signal C: bonus per moving average the close is above
  const double price = bars.close[n - 1];
  double ma_pts = 0.0;
  for (double ma : {ind.ma20[n - 1], ind.ma50[n - 1], ind.ma200[n - 1]}) {
    if (!std::isnan(ma) && price > ma) ma_pts += cfg.ma_bonus_per_level;
  }

  const double raw = (r.mom_A ? cfg.l2_momentum_a_score : 0.0) +
                     (r.mom_B ? cfg.l2_momentum_b_score : 0.0) + ma_pts;
  const double mom = (1.0 < raw) ? 1.0 : raw;   // Python min(raw, 1.0)

  r.ma_pts = py_round(ma_pts, 2);
  r.roc10 = std::isnan(roc10) ? kNaN : py_round(roc10, 3);   // NaN means None
  r.adx = std::isnan(adx) ? kNaN : py_round(adx, 2);
  r.di_p = std::isnan(di_p) ? kNaN : py_round(di_p, 2);
  r.di_n = std::isnan(di_n) ? kNaN : py_round(di_n, 2);
  r.dist_days = dist;
  r.close_pct = py_round(close_pct, 3);
  return mom;
}

}  // namespace

Layer1Result score_layer1(const BarSeries& bars, int block_trades, const ScoringConfig& cfg) {
  Layer1Result r;
  r.block_trades = block_trades;
  const auto lookback = static_cast<std::size_t>(cfg.obv_lookback_days);
  const std::size_t n = bars.size();
  if (n < lookback + 2) return r;   // Python: {"score": 0.0, "signals": {}}

  // Volume surge vs the 20 bars before today
  const double today_vol = bars.volume[n - 1];
  const double avg_vol = nan_mean(bars.volume.subspan(n - 1 - lookback, lookback));
  const double vol_ratio = avg_vol > 0 ? today_vol / avg_vol : 0.0;
  r.A = vol_ratio >= cfg.volume_surge_ratio;
  r.B = vol_ratio > cfg.rvol_threshold;

  // Up/down volume over the last 20 bars, using the full-series diff
  double up_vol = 0.0, down_vol = 0.0;
  for (std::size_t i = n - lookback; i < n; ++i) {
    const double diff = bars.close[i] - bars.close[i - 1];
    if (std::isnan(bars.volume[i])) continue;
    if (diff > 0) up_vol += bars.volume[i];
    if (diff < 0) down_vol += bars.volume[i];
  }
  const double up_down = up_vol / ((1.0 > down_vol) ? 1.0 : down_vol);   // max(down_vol, 1.0)
  r.C = up_down > cfg.up_down_vol_ratio_min;

  // OBV at or above its 20-bar high
  std::vector<double> obv(n);
  ind::obv(bars.close, bars.volume, obv);
  r.D = obv[n - 1] >= nan_max(last(obv, lookback));

  r.E = block_trades > 0;
  r.signals_fired = int(r.A) + int(r.B) + int(r.C) + int(r.D) + int(r.E);
  r.score = r.signals_fired >= cfg.layer1_min_signals ? 1.0 : 0.0;
  r.vol_ratio = py_round(vol_ratio, 3);
  r.up_down_ratio = np_round(up_down, 3);   // numpy.float64 in Python
  r.valid = true;
  return r;
}

Layer2Result score_layer2(const BarSeries& bars, const std::optional<SectorContext>& sector,
                          const ScoringConfig& cfg) {
  Layer2Result r;
  const std::size_t n = bars.size();
  if (n < kL2MinRows) return r;   // Python's _empty

  const Indicators ind = add_indicators(bars, cfg);
  const double avg_vol_20d = nan_mean(bars.volume.subspan(n - 21, 20));   // iloc[-21:-1]

  // Archetype: first match wins
  if (arch1_uptrend(bars, ind, cfg)) {
    r.archetype = 1;
  } else if (arch2_golden_cross(bars, ind, cfg)) {
    r.archetype = 2;
  } else if (arch3_breakout(bars, avg_vol_20d, cfg, r)) {
    r.archetype = 3;
  } else if (arch4_sector_rotation(sector)) {
    r.archetype = 4;
  }
  const double arch_score = r.archetype ? 1.0 : 0.0;

  const double mom = momentum(bars, ind, cfg, r);
  r.archetype_score = arch_score;
  r.momentum_score = py_round(mom, 2);
  r.total = py_round(arch_score + mom, 2);
  r.valid = true;
  return r;
}

}  // namespace mscan
