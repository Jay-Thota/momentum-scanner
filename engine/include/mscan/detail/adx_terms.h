#pragma once
// Per-bar terms of layer2_structure._compute_adx, shared by the batch adx
// kernel and StreamingIndicators so both compute them with the same
// expressions. Pass NaN for the previous bar on the first bar: pandas'
// shift(1) is NaN there, and the comparisons below then give the same 0.0 /
// NaN-skipping results the Python does.

#include <cmath>
#include <limits>

namespace mscan::ind::detail {

// max(a, b, c) skipping NaN, NaN if all three are NaN: DataFrame.max(axis=1).
inline double nan_max3(double a, double b, double c) {
  double m = -std::numeric_limits<double>::infinity();
  bool any = false;
  for (double v : {a, b, c}) {
    if (!std::isnan(v)) {
      any = true;
      if (v > m) m = v;
    }
  }
  return any ? m : std::numeric_limits<double>::quiet_NaN();
}

// tr = max(h - l, |h - c[-1]|, |l - c[-1]|), NaN-skipping
inline double true_range(double h, double l, double prev_close) {
  return nan_max3(h - l, std::fabs(h - prev_close), std::fabs(l - prev_close));
}

// np.where(cond, move, 0.0): comparisons with NaN are false, so 0.0, never NaN
inline double plus_dm(double h, double l, double prev_h, double prev_l) {
  const double up = h - prev_h, down = prev_l - l;
  return (up > down && up > 0) ? up : 0.0;
}

inline double minus_dm(double h, double l, double prev_h, double prev_l) {
  const double up = h - prev_h, down = prev_l - l;
  return (down > up && down > 0) ? down : 0.0;
}

inline double dx(double plus_di, double minus_di) {
  return 100.0 * std::fabs(plus_di - minus_di) / (plus_di + minus_di);
}

}  // namespace mscan::ind::detail
