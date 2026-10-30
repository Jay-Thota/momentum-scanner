// py_round / np_round against Python's own results (scripts/dump_rounding_golden.py).

#include <gtest/gtest.h>

#include <cmath>
#include <cstdlib>
#include <fstream>
#include <random>
#include <sstream>
#include <string>
#include <vector>

#include "mscan/pyround.h"

namespace {

// Same value, and for zero the same sign (round(-0.0001, 3) is -0.0 in Python).
bool same(double a, double b) { return a == b && std::signbit(a) == std::signbit(b); }

}  // namespace

TEST(Rounding, MatchesPythonAndNumpy) {
  std::ifstream in(std::string(MSCAN_ENGINE_DIR) + "/tests/golden/rounding.csv");
  ASSERT_TRUE(in) << "run: python -m scripts.dump_rounding_golden";
  std::string line;
  std::getline(in, line);   // header: x,py2,py3,np2,np3
  std::size_t rows = 0, bad = 0;
  while (std::getline(in, line)) {
    std::vector<double> v;
    std::stringstream ss(line);
    for (std::string cell; std::getline(ss, cell, ',');) v.push_back(std::strtod(cell.c_str(), nullptr));
    ASSERT_EQ(v.size(), 5u) << line;
    const double x = v[0];
    const bool ok = same(mscan::py_round(x, 2), v[1]) && same(mscan::py_round(x, 3), v[2]) &&
                    same(mscan::np_round(x, 2), v[3]) && same(mscan::np_round(x, 3), v[4]);
    if (!ok && bad++ < 10) {
      ADD_FAILURE() << "x=" << line.substr(0, line.find(',')) << " py2=" << mscan::py_round(x, 2)
                    << " py3=" << mscan::py_round(x, 3) << " np2=" << mscan::np_round(x, 2)
                    << " np3=" << mscan::np_round(x, 3) << " want " << line;
    }
    ++rows;
  }
  EXPECT_GT(rows, 10000u);
  EXPECT_EQ(bad, 0u) << bad << " of " << rows << " rows differ";
}

TEST(Rounding, KnownDisagreements) {
  // The cases that make two helpers necessary
  EXPECT_EQ(mscan::py_round(2.675, 2), 2.67);   // exact value is 2.67499999...
  EXPECT_EQ(mscan::np_round(2.675, 2), 2.68);   // 2.675 * 100 rounds up to 267.5
  EXPECT_TRUE(std::isnan(mscan::py_round(NAN, 3)));
  EXPECT_TRUE(std::isnan(mscan::np_round(NAN, 3)));
}

// The fma fast path against the decimal round trip (to_chars + from_chars,
// what CPython does). Random values over many magnitudes, plus the inputs the
// fast path has to get exactly right: decimal ties (k + 0.5) / 10^n and the
// doubles on either side of them, where only the sign of the exact product's
// rounding error decides.
TEST(Rounding, FastPathMatchesDecimalRoundTrip) {
  std::mt19937_64 rng(42);
  std::uniform_real_distribution<double> mant(-1.0, 1.0);
  std::uniform_int_distribution<int> expo(-12, 18);
  std::uniform_int_distribution<long long> ints(-10'000'000, 10'000'000);
  std::size_t checked = 0, bad = 0;
  auto check = [&](double x, int n) {
    ++checked;
    const double fast = mscan::py_round(x, n), slow = mscan::py_round_decimal(x, n);
    if (!same(fast, slow) && bad++ < 10) {
      ADD_FAILURE() << std::hexfloat << "x=" << x << " n=" << n << " fast=" << fast
                    << " decimal=" << slow;
    }
  };
  for (int n = 0; n <= 6; ++n) {
    for (int i = 0; i < 400'000; ++i) check(mant(rng) * std::pow(10.0, expo(rng)), n);
    for (int i = 0; i < 200'000; ++i) {
      const double tie = (static_cast<double>(ints(rng)) + 0.5) / std::pow(10.0, n);
      check(tie, n);
      check(std::nextafter(tie, INFINITY), n);
      check(std::nextafter(tie, -INFINITY), n);
    }
  }
  for (double z : {0.0, -0.0, 1e-310, -1e-310, 0.004, -0.004, 0.005, -0.005, 4.5e15, -4.5e15, 1e300}) {
    for (int n = 0; n <= 6; ++n) check(z, n);
  }
  EXPECT_EQ(bad, 0u) << bad << " of " << checked << " differ";
}
