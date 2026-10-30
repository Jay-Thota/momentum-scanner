#pragma once
// Python- and NumPy-compatible rounding for the values the layers report.
//
// The Python layers round their outputs with round(x, n), and which round()
// runs depends on the type: a plain Python float rounds the exact binary value
// to the nearest n-place decimal, while a numpy.float64 computes
// rint(x * 10^n) / 10^n. They disagree on ~2% of inputs (2.675 -> 2.67 vs
// 2.68), so each reported field must use the same one Python uses.
// tests/rounding_test.cpp checks both against tests/golden/rounding.csv.

#include <cmath>
#include <cstdio>
#include <cstdlib>

namespace mscan {

// round(x, ndigits) on a Python float. CPython formats the correctly rounded
// decimal with dtoa and parses it back; printf("%.*f") + strtod do the same
// with a correctly rounding libc. A 512-byte stack buffer fits any double at
// up to 6 decimals (the largest is ~310 digits), so this never allocates.
inline double py_round(double x, int ndigits) {
  if (!std::isfinite(x)) return x;
  char buf[512];
  std::snprintf(buf, sizeof buf, "%.*f", ndigits, x);
  return std::strtod(buf, nullptr);
}

// round(x, ndigits) on a numpy.float64: multiply by 10^n, round half to even
// (rint, under the default rounding mode), divide by 10^n.
inline double np_round(double x, int ndigits) {
  static constexpr double kPow10[] = {1.0, 10.0, 100.0, 1000.0, 10000.0, 100000.0, 1000000.0};
  const double f = kPow10[ndigits];
  return std::rint(x * f) / f;
}

}  // namespace mscan
