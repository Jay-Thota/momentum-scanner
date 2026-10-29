#include "mscan/indicators.h"

#include <cmath>
#include <limits>
#include <stdexcept>

namespace mscan::ind {
namespace {

constexpr double kNaN = std::numeric_limits<double>::quiet_NaN();

void require_same_size(std::size_t in, std::size_t out, const char* fn) {
  if (in != out) throw std::invalid_argument(std::string(fn) + ": out.size() must equal input size");
}

void require_period(int period, const char* fn) {
  if (period < 1) throw std::invalid_argument(std::string(fn) + ": period must be >= 1");
}

// State of pandas rolling(window).mean(), bit for bit. A port of roll_mean in
// pandas/_libs/window/aggregations.pyx (v3.0.3, lines 198-316); line numbers
// below refer to that file. The caller adds each value as it enters the
// window and removes it as it leaves, so every step is O(1) and the state is
// a handful of scalars: no buffer, no allocation.
//
// Relies on -ffp-contract=off and no -ffast-math: either would let the
// compiler rewrite the Kahan steps and break parity.
class PandasRollingMean {
 public:
  explicit PandasRollingMean(std::size_t window) : window_(window) {}

  // add_mean (221-250)
  void add(double v) {
    if (std::isnan(v)) return;
    ++nobs_;
    const double y = v - comp_add_;
    const double t = sum_ + y;
    comp_add_ = (t - sum_) - y;
    sum_ = t;
    if (std::signbit(v)) ++neg_ct_;
    same_run_ = (v == prev_) ? same_run_ + 1 : 1;   // GH#42064
    prev_ = v;
  }

  // remove_mean (253-266)
  void remove(double v) {
    if (std::isnan(v)) return;
    --nobs_;
    const double y = -v - comp_remove_;
    const double t = sum_ + y;
    comp_remove_ = (t - sum_) - y;
    sum_ = t;
    if (std::signbit(v)) --neg_ct_;
  }

  // calc_mean (198-218), with min_periods == window
  double value() const {
    if (nobs_ < window_ || nobs_ == 0) return kNaN;
    if (same_run_ >= nobs_) return prev_;   // window is one repeated value
    const double r = sum_ / static_cast<double>(nobs_);
    if (neg_ct_ == 0 && r < 0) return 0.0;        // all inputs >= 0
    if (neg_ct_ == nobs_ && r > 0) return 0.0;    // all inputs negative
    return r;
  }

 private:
  std::size_t window_;
  double sum_ = 0.0;
  double comp_add_ = 0.0;      // Kahan error carried by additions   (292)
  double comp_remove_ = 0.0;   // ... and separately by removals     (292)
  std::size_t nobs_ = 0;       // non-NaN values in the window
  std::size_t neg_ct_ = 0;     // how many of them have the sign bit set
  std::size_t same_run_ = 0;   // length of the current run of equal values
  // pandas seeds this with values[0] (294). NaN behaves identically: the
  // first non-NaN add sets same_run_ to 1 either way.
  double prev_ = kNaN;
};

// max(a, b, c) skipping NaN, NaN if all three are NaN: DataFrame.max(axis=1).
double nan_max3(double a, double b, double c) {
  double m = -std::numeric_limits<double>::infinity();
  bool any = false;
  for (double v : {a, b, c}) {
    if (!std::isnan(v)) {
      any = true;
      if (v > m) m = v;
    }
  }
  return any ? m : kNaN;
}

}  // namespace

// pandas rolling(period).mean() of x. min_periods defaults to the window, so
// a window with any NaN in it yields NaN.
void sma(std::span<const double> x, int period, std::span<double> out) {
  require_period(period, "sma");
  require_same_size(x.size(), out.size(), "sma");
  const auto p = static_cast<std::size_t>(period);
  PandasRollingMean mean(p);
  for (std::size_t i = 0; i < x.size(); ++i) {
    if (i >= p) mean.remove(x[i - p]);
    mean.add(x[i]);
    out[i] = mean.value();
  }
}

// Python: (close - close.shift(period)) / close.shift(period) * 100
// Same operation order, so results are bit-identical. NaN propagates through
// the arithmetic, so a NaN on either end of the window gives NaN.
void roc(std::span<const double> close, int period, std::span<double> out) {
  require_period(period, "roc");
  require_same_size(close.size(), out.size(), "roc");
  const auto p = static_cast<std::size_t>(period);
  for (std::size_t i = 0; i < close.size(); ++i) {
    if (i < p) {
      out[i] = kNaN;
    } else {
      const double prev = close[i - p];
      out[i] = (close[i] - prev) / prev * 100.0;
    }
  }
}

// Python (fetcher_price._compute_obv): direction is +1 / -1 / 0 from comparing
// each close with the previous one (comparisons with NaN are false, so 0),
// then (direction * volume).cumsum(). pandas' cumsum skips NaN: the output is
// NaN at that bar and the running total carries on unchanged.
void obv(std::span<const double> close, std::span<const double> volume,
         std::span<double> out) {
  require_same_size(close.size(), volume.size(), "obv");
  require_same_size(close.size(), out.size(), "obv");
  double total = 0.0;
  for (std::size_t i = 0; i < close.size(); ++i) {
    double direction = 0.0;
    if (i > 0 && close[i] > close[i - 1]) direction = 1.0;
    if (i > 0 && close[i] < close[i - 1]) direction = -1.0;
    const double flow = direction * volume[i];
    if (std::isnan(flow)) {
      out[i] = kNaN;
    } else {
      total += flow;
      out[i] = total;
    }
  }
}

// Python: layer2_structure._compute_adx. Rolling MEAN everywhere (not Wilder
// smoothing), matching the reference exactly:
//   tr       = max(h - l, |h - c[-1]|, |l - c[-1]|), NaN-skipping
//   +DM/-DM  = up/down move if it is the larger one and > 0, else 0 (never NaN)
//   +DI/-DI  = 100 * (rolling_mean(DM) / rolling_mean(tr))
//   dx       = 100 * |+DI - -DI| / (+DI + -DI)
//   adx      = rolling_mean(dx)
//
// Single pass, no scratch buffers. A rolling mean only needs the value that
// leaves the window, and every intermediate is cheap to recompute for bar
// i - period: tr and DM from the bars, dx from the +DI/-DI already written
// to the outputs.
void adx(const BarSeries& bars, int period, AdxOut out) {
  require_period(period, "adx");
  const std::size_t n = bars.size();
  require_same_size(n, bars.high.size(), "adx");
  require_same_size(n, bars.low.size(), "adx");
  require_same_size(n, out.adx.size(), "adx");
  require_same_size(n, out.plus_di.size(), "adx");
  require_same_size(n, out.minus_di.size(), "adx");
  const auto p = static_cast<std::size_t>(period);
  const auto& h = bars.high;
  const auto& l = bars.low;
  const auto& c = bars.close;

  // Bar 0 has no previous bar: pandas' shift(1) gives NaN there.
  auto true_range = [&](std::size_t i) {
    const double prev_close = i > 0 ? c[i - 1] : kNaN;
    return nan_max3(h[i] - l[i], std::fabs(h[i] - prev_close), std::fabs(l[i] - prev_close));
  };
  // np.where(cond, move, 0.0): comparisons with NaN are false, so 0.0
  auto plus_dm = [&](std::size_t i) {
    if (i == 0) return 0.0;
    const double up = h[i] - h[i - 1], down = l[i - 1] - l[i];
    return (up > down && up > 0) ? up : 0.0;
  };
  auto minus_dm = [&](std::size_t i) {
    if (i == 0) return 0.0;
    const double up = h[i] - h[i - 1], down = l[i - 1] - l[i];
    return (down > up && down > 0) ? down : 0.0;
  };
  auto dx = [&](std::size_t i) {
    const double pdi = out.plus_di[i], mdi = out.minus_di[i];
    return 100.0 * std::fabs(pdi - mdi) / (pdi + mdi);
  };

  PandasRollingMean atr(p), plus_mean(p), minus_mean(p), dx_mean(p);
  for (std::size_t i = 0; i < n; ++i) {
    if (i >= p) {
      atr.remove(true_range(i - p));
      plus_mean.remove(plus_dm(i - p));
      minus_mean.remove(minus_dm(i - p));
    }
    atr.add(true_range(i));
    plus_mean.add(plus_dm(i));
    minus_mean.add(minus_dm(i));

    const double a = atr.value();
    out.plus_di[i] = 100.0 * (plus_mean.value() / a);
    out.minus_di[i] = 100.0 * (minus_mean.value() / a);

    if (i >= p) dx_mean.remove(dx(i - p));   // reads outputs already written
    dx_mean.add(dx(i));
    out.adx[i] = dx_mean.value();
  }
}

}  // namespace mscan::ind
