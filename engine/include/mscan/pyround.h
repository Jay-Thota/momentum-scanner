#pragma once
// Python- and NumPy-compatible rounding for the values the layers report.
//
// The Python layers round their outputs with round(x, n), and which round()
// runs depends on the type: a plain Python float rounds the exact binary value
// to the nearest n-place decimal, while a numpy.float64 computes
// rint(x * 10^n) / 10^n. They disagree on ~2% of inputs (2.675 -> 2.67 vs
// 2.68), so each reported field must use the same one Python uses.
// tests/rounding_test.cpp checks both against tests/golden/rounding.csv.

#include <charconv>
#include <cmath>

namespace mscan {

namespace detail {
inline constexpr double kPow10[] = {1.0, 10.0, 100.0, 1000.0, 10000.0, 100000.0, 1000000.0};
}  // namespace detail

// round(x, ndigits) on a Python float the way CPython does it: format the
// correctly rounded decimal and parse it back. std::to_chars and
// std::from_chars are specified as printf("%.*f") and strtod in the "C"
// locale, both correctly rounded. Unlike snprintf/strtod they never touch the
// global locale, whose shared state made threads contend. A 512-byte stack
// buffer fits any double at up to 6 decimals (the largest is ~310 digits),
// so this never allocates.
inline double py_round_decimal(double x, int ndigits) {
  if (!std::isfinite(x)) return x;
  char buf[512];
  const char* end = std::to_chars(buf, buf + sizeof buf, x, std::chars_format::fixed, ndigits).ptr;
  double out = 0.0;
  std::from_chars(buf, end, out);
  return out;
}

// round(x, ndigits) on a Python float, without the decimal round trip.
//
// Python rounds the EXACT value of x * 10^n to the nearest integer k (ties to
// even), then returns the double nearest k / 10^n. hi = x * p rounds, but
// fma(x, p, -hi) is the exact rounding error, so x * p == hi + lo exactly.
// k = rint(hi) is already right unless hi sits exactly halfway (t = +-0.5),
// where only the sign of lo says which side of the tie the exact value is on;
// elsewhere |lo| <= ulp(hi) / 2 cannot move the value across a half. Last,
// k and p are exact doubles, so the IEEE division k / p is the double nearest
// k / 10^n. Falls back to the decimal path when hi is too large for
// fractions (|hi| >= 2^52) or ndigits is outside the table.
// tests/rounding_test.cpp compares the two on millions of values and ties.
inline double py_round(double x, int ndigits) {
  if (!std::isfinite(x)) return x;
  if (ndigits < 0 || ndigits > 6) return py_round_decimal(x, ndigits);
  const double p = detail::kPow10[ndigits];
  const double hi = x * p;
  if (!(std::fabs(hi) < 4503599627370496.0)) return py_round_decimal(x, ndigits);   // 2^52
  const double lo = std::fma(x, p, -hi);
  double k = std::rint(hi);   // ties to even under the default rounding mode
  const double t = hi - k;    // exact: k is within 0.5 of hi
  if (t == 0.5 && lo > 0) k += 1.0;    // exact value just above the tie
  if (t == -0.5 && lo < 0) k -= 1.0;   // ... just below it
  return k / p;
}

// round(x, ndigits) on a numpy.float64: multiply by 10^n, round half to even
// (rint, under the default rounding mode), divide by 10^n.
inline double np_round(double x, int ndigits) {
  const double f = detail::kPow10[ndigits];
  return std::rint(x * f) / f;
}

}  // namespace mscan
