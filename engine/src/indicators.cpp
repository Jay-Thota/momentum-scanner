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

}  // namespace

// pandas rolling(period).mean(), bit for bit. This is a port of roll_mean in
// pandas/_libs/window/aggregations.pyx (v3.0.3, lines 198-316); line numbers
// below refer to that file. min_periods defaults to the window, so a window
// with any NaN in it yields NaN.
//
// O(1) per bar: each value is added once when it enters the window and
// removed once when it leaves. All state lives in locals, so no allocation.
// Relies on -ffp-contract=off and no -ffast-math: either would let the
// compiler rewrite the Kahan steps and break parity.
void sma(std::span<const double> x, int period, std::span<double> out) {
  require_period(period, "sma");
  require_same_size(x.size(), out.size(), "sma");
  const auto p = static_cast<std::size_t>(period);

  double sum = 0.0;
  double comp_add = 0.0;      // Kahan error carried by additions   (292)
  double comp_remove = 0.0;   // ... and separately by removals     (292)
  std::size_t nobs = 0;       // non-NaN values in the window
  std::size_t neg_ct = 0;     // how many of them have the sign bit set
  std::size_t same_run = 0;   // length of the current run of equal values
  double prev = x.empty() ? 0.0 : x[0];   // prev_value = values[s]  (294)

  for (std::size_t i = 0; i < x.size(); ++i) {
    // remove_mean (253-266): the value leaving the window, unless it is NaN
    if (i >= p) {
      const double v = x[i - p];
      if (!std::isnan(v)) {
        --nobs;
        const double y = -v - comp_remove;
        const double t = sum + y;
        comp_remove = (t - sum) - y;
        sum = t;
        if (std::signbit(v)) --neg_ct;
      }
    }

    // add_mean (221-250): the value entering the window, unless it is NaN
    const double v = x[i];
    if (!std::isnan(v)) {
      ++nobs;
      const double y = v - comp_add;
      const double t = sum + y;
      comp_add = (t - sum) - y;
      sum = t;
      if (std::signbit(v)) ++neg_ct;
      same_run = (v == prev) ? same_run + 1 : 1;   // GH#42064 (244-250)
      prev = v;
    }

    // calc_mean (198-218)
    if (nobs >= p && nobs > 0) {
      double r = sum / static_cast<double>(nobs);
      if (same_run >= nobs) {
        r = prev;                        // window is one repeated value
      } else if (neg_ct == 0 && r < 0) {
        r = 0.0;                         // all inputs >= 0, mean can't be < 0
      } else if (neg_ct == nobs && r > 0) {
        r = 0.0;                         // all inputs negative, mean can't be > 0
      }
      out[i] = r;
    } else {
      out[i] = kNaN;
    }
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

}  // namespace mscan::ind
