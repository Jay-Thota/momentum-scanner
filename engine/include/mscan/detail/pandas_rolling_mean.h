#pragma once
// State of pandas rolling(window).mean(), bit for bit. A port of roll_mean in
// pandas/_libs/window/aggregations.pyx (v3.0.3, lines 198-316); line numbers
// below refer to that file. The caller adds each value as it enters the
// window and removes it as it leaves, so every step is O(1) and the state is
// a handful of scalars: no buffer, no allocation.
//
// Shared by the batch sma/adx kernels and StreamingIndicators. Both feed it
// the same values in the same order, which is why their outputs are identical.
//
// Relies on -ffp-contract=off and no -ffast-math: either would let the
// compiler rewrite the Kahan steps and break parity.

#include <cmath>
#include <cstddef>
#include <limits>

namespace mscan::ind::detail {

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
    if (nobs_ < window_ || nobs_ == 0) return std::numeric_limits<double>::quiet_NaN();
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
  double prev_ = std::numeric_limits<double>::quiet_NaN();
};

}  // namespace mscan::ind::detail
