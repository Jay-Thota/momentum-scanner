// StreamingIndicators vs the batch kernels.
//
// The batch kernels already match Python bit for bit (parity_test.cpp), so
// streaming only has to match them. Each check streams a series bar by bar
// and compares every value, at every bar, with the batch output at that
// index: bit-identical, or NaN on both sides.

#include <gtest/gtest.h>

#include <algorithm>
#include <bit>
#include <cmath>
#include <cstdint>
#include <filesystem>
#include <limits>
#include <random>
#include <sstream>
#include <string>
#include <vector>

#include "alloc_counter.h"
#include "golden.h"
#include "mscan/indicators.h"
#include "mscan/streaming.h"
#include "mscan/universe.h"

namespace {

namespace ind = mscan::ind;
constexpr double kNaN = std::numeric_limits<double>::quiet_NaN();

bool same(double a, double b) {
  return (std::isnan(a) && std::isnan(b)) || std::bit_cast<uint64_t>(a) == std::bit_cast<uint64_t>(b);
}

double nan_max(const std::vector<double>& x, std::size_t begin, std::size_t end) {
  double m = kNaN;
  for (std::size_t i = begin; i < end; ++i) {
    if (!std::isnan(x[i]) && (std::isnan(m) || x[i] > m)) m = x[i];
  }
  return m;
}

// Streams `bars` and returns a description of the first value that differs
// from the batch kernels, or "" when every value at every bar matches.
std::string first_mismatch(const mscan::BarSeries& bars, int roc_period, int obv_window) {
  const std::size_t n = bars.size();
  std::vector<double> ma20(n), ma50(n), ma200(n), roc(n), adx(n), pdi(n), mdi(n), obv(n);
  ind::sma(bars.close, 20, ma20);
  ind::sma(bars.close, 50, ma50);
  ind::sma(bars.close, 200, ma200);
  ind::roc(bars.close, roc_period, roc);
  ind::adx(bars, 14, {adx, pdi, mdi});
  ind::obv(bars.close, bars.volume, obv);

  ind::StreamingIndicators s(roc_period, obv_window);
  const auto w = static_cast<std::size_t>(obv_window);
  for (std::size_t i = 0; i < n; ++i) {
    s.update(bars.open[i], bars.high[i], bars.low[i], bars.close[i], bars.volume[i]);
    auto lag = [&](const std::vector<double>& x, std::size_t k) { return i >= k ? x[i - k] : kNaN; };
    const std::pair<const char*, std::pair<double, double>> checks[] = {
        {"sma20", {s.sma20(), ma20[i]}},
        {"sma50", {s.sma50(), ma50[i]}},
        {"sma200", {s.sma200(), ma200[i]}},
        {"sma50_ago(5)", {s.sma50_ago(5), lag(ma50, 5)}},
        {"sma200_ago(10)", {s.sma200_ago(10), lag(ma200, 10)}},
        {"sma200_ago(63)", {s.sma200_ago(63), lag(ma200, 63)}},
        {"roc", {s.roc(), roc[i]}},
        {"adx", {s.adx(), adx[i]}},
        {"plus_di", {s.plus_di(), pdi[i]}},
        {"minus_di", {s.minus_di(), mdi[i]}},
        {"obv", {s.obv(), obv[i]}},
        {"obv_high", {s.obv_high(), nan_max(obv, i + 1 - std::min(w, i + 1), i + 1)}},
    };
    for (const auto& [name, v] : checks) {
      if (!same(v.first, v.second)) {
        std::ostringstream os;
        os.precision(17);
        os << name << " at bar " << i << ": streaming " << v.first << ", batch " << v.second;
        return os.str();
      }
    }
  }
  return "";
}

// Random walk with the awkward cases the rolling mean has special rules for:
// flat runs (same-value rule), NaN holes in each field independently, zero
// and NaN volume.
struct Synthetic {
  std::vector<double> o, h, l, c, v;
  mscan::BarSeries bars() const { return {o, h, l, c, v}; }
};

Synthetic synthetic(std::size_t n, unsigned seed) {
  std::mt19937_64 rng(seed);
  std::uniform_real_distribution<double> u(0.0, 1.0);
  Synthetic s;
  double price = 50.0;
  for (std::size_t i = 0; i < n; ++i) {
    if (u(rng) > 0.1) price *= 1.0 + (u(rng) - 0.5) * 0.06;   // else a flat bar
    const double spread = price * 0.02 * u(rng);
    s.o.push_back(price);
    s.h.push_back(price + spread);
    s.l.push_back(price - spread);
    s.c.push_back(price);
    s.v.push_back(u(rng) < 0.05 ? 0.0 : std::round(1e6 * u(rng)));
    if (u(rng) < 0.01) s.c.back() = kNaN;
    if (u(rng) < 0.01) s.h.back() = kNaN;
    if (u(rng) < 0.01) s.l.back() = kNaN;
    if (u(rng) < 0.01) s.v.back() = kNaN;
  }
  return s;
}

}  // namespace

TEST(RingTest, KeepsTheLastNValues) {
  ind::Ring<4> r;
  EXPECT_TRUE(std::isnan(r.ago(0)));
  for (int i = 1; i <= 6; ++i) r.push(i);
  EXPECT_EQ(r.ago(0), 6);
  EXPECT_EQ(r.ago(3), 3);
  EXPECT_TRUE(std::isnan(r.ago(4)));   // only 4 slots
}

TEST(RollingMaxTest, MatchesBruteForceWithNaN) {
  std::mt19937_64 rng(7);
  std::uniform_real_distribution<double> u(0.0, 1.0);
  for (std::size_t w : {1u, 3u, 20u, 64u}) {
    ind::RollingMax<64> m(w);
    std::vector<double> x;
    for (std::size_t i = 0; i < 2000; ++i) {
      // Small integer range so equal values are common; ~20% NaN
      x.push_back(u(rng) < 0.2 ? kNaN : std::floor(u(rng) * 10.0));
      m.push(x.back());
      const double want = nan_max(x, i + 1 - std::min(w, i + 1), i + 1);
      ASSERT_TRUE(same(m.max(), want)) << "window " << w << " index " << i;
    }
  }
}

TEST(StreamingTest, RejectsPeriodsOutsideTheBuffers) {
  EXPECT_THROW(ind::StreamingIndicators(0, 20), std::invalid_argument);
  EXPECT_THROW(ind::StreamingIndicators(256, 20), std::invalid_argument);
  EXPECT_THROW(ind::StreamingIndicators(10, 65), std::invalid_argument);
  EXPECT_NO_THROW(ind::StreamingIndicators(255, 64));
}

TEST(StreamingTest, MatchesBatchOnSyntheticSeries) {
  for (unsigned seed = 1; seed <= 20; ++seed) {
    const Synthetic s = synthetic(600, seed);
    EXPECT_EQ(first_mismatch(s.bars(), 10, 20), "") << "seed " << seed;
  }
  // Edge periods: the smallest and largest the buffers allow
  const Synthetic s = synthetic(600, 99);
  EXPECT_EQ(first_mismatch(s.bars(), 1, 1), "");
  EXPECT_EQ(first_mismatch(s.bars(), 255, 64), "");
}

TEST(StreamingTest, MatchesBatchOnGoldenSeries) {
  ASSERT_GE(mscan_test::goldens().size(), 68u);
  for (const auto& g : mscan_test::goldens()) {
    EXPECT_EQ(first_mismatch(mscan_test::bars_of(g), 10, 20), "") << g.name;
  }
}

// Every ticker of the real 4-year file, from its first bar to the last day.
// Skipped when engine/data/history.bin has not been exported (CI).
TEST(StreamingTest, MatchesBatchOnRealUniverse) {
  const std::string path = std::string(MSCAN_ENGINE_DIR) + "/data/history.bin";
  if (!std::filesystem::exists(path)) GTEST_SKIP() << "no " << path;
  const auto u = mscan::Universe::open_mmap(path);
  std::size_t bad = 0;
  for (std::size_t i = 0; i < u.num_tickers(); ++i) {
    const std::string diff = first_mismatch(u.series(i, u.num_days() - 1), 10, 20);
    if (!diff.empty() && bad++ < 5) ADD_FAILURE() << u.ticker(i) << ": " << diff;
  }
  EXPECT_EQ(bad, 0u) << "of " << u.num_tickers() << " tickers";
}

TEST(StreamingTest, UpdateNoHeapAllocation) {
  const Synthetic s = synthetic(1000, 3);
  ind::StreamingIndicators st(10, 20);
  const long before = mscan_test::allocations();
  for (std::size_t i = 0; i < s.c.size(); ++i) st.update(s.o[i], s.h[i], s.l[i], s.c[i], s.v[i]);
  EXPECT_EQ(mscan_test::allocations() - before, 0) << "update allocated on the heap";
}
