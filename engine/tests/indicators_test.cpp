// Hand-computed indicator cases. Every expected value was produced by the
// Python reference (pandas rolling().mean(), layer2_structure._compute_roc,
// fetcher_price._compute_obv), so these pin down pandas semantics exactly.

#include <gtest/gtest.h>

#include <atomic>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <limits>
#include <new>
#include <stdexcept>
#include <vector>

#include "mscan/indicators.h"

// ---------------------------------------------------------------------------
// Allocation counter: replaces global operator new for this test binary so a
// test can assert a kernel call performs zero heap allocations.
// ---------------------------------------------------------------------------
namespace {
std::atomic<long> g_allocations{0};
}

void* operator new(std::size_t n) {
  ++g_allocations;
  if (void* p = std::malloc(n == 0 ? 1 : n)) return p;
  throw std::bad_alloc();
}
void* operator new[](std::size_t n) { return ::operator new(n); }
void operator delete(void* p) noexcept { std::free(p); }
void operator delete[](void* p) noexcept { std::free(p); }
void operator delete(void* p, std::size_t) noexcept { std::free(p); }
void operator delete[](void* p, std::size_t) noexcept { std::free(p); }

namespace {

namespace ind = mscan::ind;
constexpr double kNaN = std::numeric_limits<double>::quiet_NaN();

// Exact comparison: NaN must line up with NaN, everything else bit-equal.
void expect_same(const std::vector<double>& got, const std::vector<double>& want) {
  ASSERT_EQ(got.size(), want.size());
  for (std::size_t i = 0; i < want.size(); ++i) {
    if (std::isnan(want[i])) {
      EXPECT_TRUE(std::isnan(got[i])) << "index " << i << ": want NaN, got " << got[i];
    } else {
      EXPECT_EQ(got[i], want[i]) << "index " << i;
    }
  }
}

std::vector<double> run_sma(const std::vector<double>& x, int period) {
  std::vector<double> out(x.size(), -1.0);   // poison: every slot must be written
  ind::sma(x, period, out);
  return out;
}

}  // namespace

// Guards the NoHeapAllocation tests: they are only meaningful if the counter
// really sees allocations. The volatile sink stops the compiler from eliding
// the new/delete pair (which C++14 allows when the result is unobservable).
TEST(AllocationCounter, CountsRealAllocations) {
  static double* volatile sink = nullptr;
  const long before = g_allocations.load();
  std::vector<double> v(100, 1.0);
  sink = v.data();
  EXPECT_GE(g_allocations.load() - before, 1);
}

TEST(Smoke, EmptyBarSeries) {
  mscan::BarSeries s;
  EXPECT_TRUE(s.empty());
  EXPECT_EQ(s.size(), 0u);
}

// ---------------------------------------------------------------------------
// SMA -- spec for the hand-written kernel. Target: pandas rolling(p).mean(),
// bit for bit (min_periods = period).
// ---------------------------------------------------------------------------

TEST(SmaTest, WarmUpBarsAreNaN) {
  expect_same(run_sma({1, 2, 3, 4, 5}, 3), {kNaN, kNaN, 2, 3, 4});
}

TEST(SmaTest, PeriodOneIsIdentity) {
  expect_same(run_sma({1.5, -2.25, 3}, 1), {1.5, -2.25, 3});
}

TEST(SmaTest, PeriodLongerThanInputIsAllNaN) {
  expect_same(run_sma({1, 2}, 5), {kNaN, kNaN});
}

TEST(SmaTest, EmptyInput) {
  std::vector<double> x, out;
  ind::sma(x, 3, out);   // must not touch anything
  SUCCEED();
}

TEST(SmaTest, NaNInsideWindowGivesNaNThenRecovers) {
  // Any window containing a NaN is NaN; once the NaN leaves, values resume.
  expect_same(run_sma({1, 2, kNaN, 4, 5, 6}, 2), {kNaN, 1.5, kNaN, kNaN, 4.5, 5.5});
}

TEST(SmaTest, LeadingNaNsLikeAnUnlistedTicker) {
  expect_same(run_sma({kNaN, kNaN, 1, 2, 3}, 2), {kNaN, kNaN, kNaN, 1.5, 2.5});
}

TEST(SmaTest, ConstantWindowIsExactlyTheValue) {
  // A plain running sum gives 100.09999999999997 / ...98 here. pandas returns
  // exactly 100.1, so "close > ma20" on a flat stock gives the same answer in
  // both languages.
  std::vector<double> x(25, 100.1);
  auto out = run_sma(x, 20);
  for (std::size_t i = 19; i < x.size(); ++i) EXPECT_EQ(out[i], 100.1) << "index " << i;
}

TEST(SmaTest, NoDriftAfterLargeValueLeavesWindow) {
  // 1e15 enters and leaves the window. A plain running sum keeps the rounding
  // error from that add/subtract forever (0.2083..., 0.3083..., ...).
  std::vector<double> x = {1e15};
  for (int k = 1; k <= 11; ++k) x.push_back(0.1 * k);
  auto out = run_sma(x, 3);
  EXPECT_EQ(out[3], 0.20000000000000004);
  EXPECT_EQ(out[4], 0.30000000000000004);
  EXPECT_EQ(out[5], 0.4000000000000001);
  EXPECT_EQ(out[6], 0.5);
}

TEST(SmaTest, InvalidArgumentsThrow) {
  std::vector<double> x = {1, 2, 3}, out(3), short_out(2);
  EXPECT_THROW(ind::sma(x, 0, out), std::invalid_argument);
  EXPECT_THROW(ind::sma(x, 2, short_out), std::invalid_argument);
}

TEST(SmaTest, NoHeapAllocation) {
  std::vector<double> x(1000), out(1000);
  for (std::size_t i = 0; i < x.size(); ++i) x[i] = 100.0 + static_cast<double>(i % 37);
  const long before = g_allocations.load();
  ind::sma(x, 200, out);
  EXPECT_EQ(g_allocations.load() - before, 0) << "sma allocated on the heap";
}

// ---------------------------------------------------------------------------
// ROC
// ---------------------------------------------------------------------------

TEST(RocTest, MatchesPythonIncludingNaN) {
  std::vector<double> c = {100.0, 105.0, 110.25, 99.0, kNaN, 120.0, 118.8};
  std::vector<double> out(c.size());
  ind::roc(c, 2, out);
  expect_same(out, {kNaN, kNaN, 10.25, -5.714285714285714, kNaN, 21.21212121212121, kNaN});
}

TEST(RocTest, InvalidArgumentsThrow) {
  std::vector<double> c = {1, 2, 3}, out(3), short_out(2);
  EXPECT_THROW(ind::roc(c, 0, out), std::invalid_argument);
  EXPECT_THROW(ind::roc(c, 1, short_out), std::invalid_argument);
}

TEST(RocTest, NoHeapAllocation) {
  std::vector<double> c(1000, 50.0), out(1000);
  const long before = g_allocations.load();
  ind::roc(c, 10, out);
  EXPECT_EQ(g_allocations.load() - before, 0);
}

// ---------------------------------------------------------------------------
// OBV
// ---------------------------------------------------------------------------

TEST(ObvTest, UpDownFlatAndNaNClose) {
  // A NaN close compares false both ways, so that bar and the next add 0.
  std::vector<double> c = {10, 11, 11, 9, kNaN, 10, 12};
  std::vector<double> v = {100, 200, 300, 400, 500, 600, 700};
  std::vector<double> out(c.size());
  ind::obv(c, v, out);
  expect_same(out, {0, 200, 200, -200, -200, -200, 500});
}

TEST(ObvTest, NaNVolumeIsSkippedNotPropagated) {
  // pandas cumsum: NaN at that bar, running total continues afterwards.
  std::vector<double> c = {10, 11, 11, 9, kNaN, 10, 12};
  std::vector<double> v = {100, 200, 300, 400, kNaN, 600, 700};
  std::vector<double> out(c.size());
  ind::obv(c, v, out);
  expect_same(out, {0, 200, 200, -200, kNaN, -200, 500});
}

TEST(ObvTest, NoHeapAllocation) {
  std::vector<double> c(1000, 50.0), v(1000, 1e6), out(1000);
  const long before = g_allocations.load();
  ind::obv(c, v, out);
  EXPECT_EQ(g_allocations.load() - before, 0);
}
