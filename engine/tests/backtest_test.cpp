// run_backtest: look-ahead guard, thread independence, regime filter and the
// return math, on synthetic universes (and the real file when it exists).

#include <gtest/gtest.h>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <filesystem>
#include <limits>
#include <random>
#include <string>
#include <thread>
#include <vector>

#include "mscan/backtest.h"
#include "mscan/thread_pool.h"
#include "mscan/universe.h"

namespace {

constexpr double kNaN = std::numeric_limits<double>::quiet_NaN();

// Owns the (T, D) arrays a Universe::from_buffers view points into.
struct Panel {
  std::vector<std::string> tickers;
  std::vector<int32_t> dates;
  std::vector<double> o, h, l, c, v;
  std::size_t days() const { return dates.size(); }
  mscan::Universe universe() const {
    return mscan::Universe::from_buffers(tickers, dates, o.data(), h.data(), l.data(), c.data(),
                                         v.data());
  }
};

// SPY plus `n` equities with random drifts, so some trend (archetype 1 plus
// momentum) and some don't, with occasional volume surges on up days for
// layer 1. A few tickers start late, like IPOs. Volume is well above the
// 1M gate.
Panel synthetic_panel(std::size_t n, std::size_t days, unsigned seed, double spy_drift = 0.0005) {
  std::mt19937_64 rng(seed);
  std::uniform_real_distribution<double> u(0.0, 1.0);
  Panel p;
  for (std::size_t d = 0; d < days; ++d) p.dates.push_back(20200101 + static_cast<int32_t>(d));
  const std::size_t total = (n + 1) * days;
  p.o.assign(total, kNaN);
  p.h = p.l = p.c = p.v = p.o;
  for (std::size_t i = 0; i <= n; ++i) {
    p.tickers.push_back(i == 0 ? "SPY" : "T" + std::to_string(i));
    const double drift = i == 0 ? spy_drift : (u(rng) - 0.35) * 0.004;
    const std::size_t first = (i > 0 && i % 7 == 0) ? days / 4 : 0;
    double price = 20.0 + 80.0 * u(rng);
    for (std::size_t d = first; d < days; ++d) {
      const double move = drift + (u(rng) - 0.5) * 0.03;
      const double open = price;
      price *= 1.0 + move;
      const std::size_t k = i * days + d;
      p.o[k] = open;
      p.c[k] = price;
      p.h[k] = std::max(open, price) * (1.0 + 0.01 * u(rng));
      p.l[k] = std::min(open, price) * (1.0 - 0.01 * u(rng));
      p.v[k] = std::round(2e6 * (0.5 + u(rng)) * (move > 0 && u(rng) < 0.15 ? 3.0 : 1.0));
    }
  }
  return p;
}

// Everything after day `cut` replaced with garbage, SPY included
Panel corrupt_after(Panel p, std::size_t cut) {
  const double garbage[] = {1e12, 0.0, -5.0, kNaN, 3.0};
  for (std::size_t i = 0; i < p.tickers.size(); ++i) {
    for (std::size_t d = cut + 1; d < p.days(); ++d) {
      const std::size_t k = i * p.days() + d;
      const double g = garbage[(i + d) % 5];
      p.o[k] = p.h[k] = p.l[k] = p.c[k] = g;
      p.v[k] = g * 1e3;
    }
  }
  return p;
}

mscan::BacktestConfig config_for(const mscan::Universe& u, double min_score) {
  mscan::BacktestConfig cfg;
  cfg.start_day = 0;
  cfg.end_day = u.num_days() - 1;
  cfg.min_pv_score = min_score;
  cfg.bear_min_pv_score = min_score + 0.5;
  cfg.fill = mscan::FillPolicy::kCloseSameDay;
  return cfg;
}

// Picks on days <= cut must be the same (day, ticker, score) in both runs
void expect_same_picks_through(const mscan::BacktestResult& a, const mscan::BacktestResult& b,
                               std::size_t cut) {
  std::vector<const mscan::Pick*> pa, pb;
  for (const auto& p : a.picks) if (p.day <= cut) pa.push_back(&p);
  for (const auto& p : b.picks) if (p.day <= cut) pb.push_back(&p);
  ASSERT_GT(pa.size(), 0u) << "no picks before day " << cut << ": the test proves nothing";
  ASSERT_EQ(pa.size(), pb.size()) << "cut " << cut;
  for (std::size_t k = 0; k < pa.size(); ++k) {
    EXPECT_EQ(pa[k]->day, pb[k]->day);
    EXPECT_EQ(pa[k]->ticker_idx, pb[k]->ticker_idx) << "day " << pa[k]->day;
    EXPECT_EQ(pa[k]->score, pb[k]->score) << "day " << pa[k]->day;
  }
}

}  // namespace

// Day-d picks must not change when every bar after d is garbage.
TEST(BacktestTest, LookAheadCorruptingTheFutureLeavesPastPicksAlone) {
  const Panel clean = synthetic_panel(60, 500, 11);
  const auto u = clean.universe();
  mscan::ThreadPool pool(4);
  const auto cfg = config_for(u, 1.5);
  const auto base = mscan::run_backtest(u, 0, cfg, pool);
  for (std::size_t cut : {250u, 320u, 410u, 498u}) {
    const Panel bad = corrupt_after(clean, cut);
    const auto ub = bad.universe();
    expect_same_picks_through(base, mscan::run_backtest(ub, 0, cfg, pool), cut);
  }
}

// Same check on the real 4-year file with the production thresholds.
TEST(BacktestTest, LookAheadOnRealUniverse) {
  const std::string path = std::string(MSCAN_ENGINE_DIR) + "/data/history.bin";
  if (!std::filesystem::exists(path)) GTEST_SKIP() << "no " << path;
  const auto mapped = mscan::Universe::open_mmap(path);
  // Copy into owned arrays so they can be corrupted
  Panel clean;
  const std::size_t days = mapped.num_days();
  for (std::size_t d = 0; d < days; ++d) clean.dates.push_back(mapped.date(d));
  clean.o.assign(mapped.num_tickers() * days, kNaN);
  clean.h = clean.l = clean.c = clean.v = clean.o;
  for (std::size_t i = 0; i < mapped.num_tickers(); ++i) {
    clean.tickers.push_back(mapped.ticker(i));
    const auto s = mapped.series(i, days - 1);
    const std::size_t first = mapped.first_valid_day(i);
    for (std::size_t k = 0; k < s.size(); ++k) {
      const std::size_t at = i * days + first + k;
      clean.o[at] = s.open[k];
      clean.h[at] = s.high[k];
      clean.l[at] = s.low[k];
      clean.c[at] = s.close[k];
      clean.v[at] = s.volume[k];
    }
  }
  const auto u = clean.universe();
  mscan::ThreadPool pool(std::max(1u, std::thread::hardware_concurrency()));
  auto cfg = config_for(u, 2.0);
  cfg.start_day = days - 260;   // the last year keeps the test quick
  const auto base = mscan::run_backtest(u, 0, cfg, pool);
  const std::size_t cut = days - 130;
  const Panel bad = corrupt_after(clean, cut);
  const auto ub = bad.universe();
  expect_same_picks_through(base, mscan::run_backtest(ub, 0, cfg, pool), cut);
}

TEST(BacktestTest, ResultsDoNotDependOnThreadCount) {
  const Panel p = synthetic_panel(40, 400, 5);
  const auto u = p.universe();
  const auto cfg = config_for(u, 1.5);
  mscan::ThreadPool one(1), many(8);
  const auto a = mscan::run_backtest(u, 0, cfg, one);
  const auto b = mscan::run_backtest(u, 0, cfg, many);
  ASSERT_EQ(a.picks.size(), b.picks.size());
  ASSERT_GT(a.picks.size(), 0u);
  for (std::size_t k = 0; k < a.picks.size(); ++k) {
    EXPECT_EQ(a.picks[k].ticker_idx, b.picks[k].ticker_idx);
    EXPECT_EQ(a.picks[k].score, b.picks[k].score);
  }
  EXPECT_EQ(a.evaluations, b.evaluations);
}

TEST(BacktestTest, PicksAreTopNAboveThresholdAndNeverSpy) {
  const Panel p = synthetic_panel(60, 400, 8);
  const auto u = p.universe();
  auto cfg = config_for(u, 1.5);
  cfg.top_n = 3;
  mscan::ThreadPool pool(2);
  const auto r = mscan::run_backtest(u, 0, cfg, pool);
  ASSERT_GT(r.picks.size(), 0u);
  std::size_t per_day = 0;
  for (std::size_t k = 0; k < r.picks.size(); ++k) {
    const auto& pk = r.picks[k];
    EXPECT_NE(pk.ticker_idx, 0u) << "SPY picked";
    EXPECT_GE(pk.score, cfg.min_pv_score);
    per_day = (k > 0 && r.picks[k - 1].day == pk.day) ? per_day + 1 : 1;
    EXPECT_LE(per_day, 3u);
    if (per_day > 1) {   // descending score, then ascending ticker
      const auto& prev = r.picks[k - 1];
      EXPECT_TRUE(prev.score > pk.score || (prev.score == pk.score && prev.ticker_idx < pk.ticker_idx));
    }
  }
}

// Random baseline: the same seed reproduces the picks exactly, whatever the
// thread count; another seed gives different ones.
TEST(BacktestTest, RandomBaselineIsReproducibleFromItsSeed) {
  const Panel p = synthetic_panel(60, 400, 8);
  const auto u = p.universe();
  auto cfg = config_for(u, 1.0);
  cfg.top_n = 3;
  cfg.selection = mscan::Selection::kRandom;
  cfg.seed = 42;
  mscan::ThreadPool one(1), many(8);
  const auto a = mscan::run_backtest(u, 0, cfg, one);
  const auto b = mscan::run_backtest(u, 0, cfg, many);
  ASSERT_GT(a.contested_days, 0u) << "no day with a choice to make: the test proves nothing";
  ASSERT_EQ(a.picks.size(), b.picks.size());
  for (std::size_t k = 0; k < a.picks.size(); ++k) {
    EXPECT_EQ(a.picks[k].day, b.picks[k].day);
    EXPECT_EQ(a.picks[k].ticker_idx, b.picks[k].ticker_idx);
  }
  ASSERT_EQ(a.stats.size(), b.stats.size());
  for (std::size_t h = 0; h < a.stats.size(); ++h) {
    EXPECT_EQ(a.stats[h].mean_excess, b.stats[h].mean_excess);
    EXPECT_EQ(a.stats[h].sharpe, b.stats[h].sharpe);
  }

  cfg.seed = 43;
  const auto c = mscan::run_backtest(u, 0, cfg, one);
  ASSERT_EQ(a.picks.size(), c.picks.size());   // same candidates, same count per day
  std::size_t differ = 0;
  for (std::size_t k = 0; k < a.picks.size(); ++k) differ += a.picks[k].ticker_idx != c.picks[k].ticker_idx;
  EXPECT_GT(differ, 0u);
}

// Every random pick is a candidate the top-score run could have picked that
// day: eligible, past the L0 gate and at or above the day's (regime)
// threshold. Each day takes min(top_n, candidates), with no repeats.
TEST(BacktestTest, RandomBaselinePicksOnlyFromTheEligibleCandidates) {
  // SPY drifts down then the regime flips often enough to hit bear days
  const Panel p = synthetic_panel(60, 500, 21, /*spy_drift=*/-0.0002);
  const auto u = p.universe();
  auto cfg = config_for(u, 1.0);
  cfg.eligible.assign(u.num_tickers(), 1);
  cfg.eligible[0] = 0;
  for (std::size_t i = 5; i < u.num_tickers(); i += 5) cfg.eligible[i] = 0;
  mscan::ThreadPool pool(4);

  auto all_cfg = cfg;   // top_n past the universe size: every candidate
  all_cfg.top_n = static_cast<int>(u.num_tickers());
  const auto all = mscan::run_backtest(u, 0, all_cfg, pool);
  std::vector<std::vector<std::uint32_t>> candidates(u.num_days());
  for (const auto& pk : all.picks) candidates[pk.day].push_back(pk.ticker_idx);

  cfg.top_n = 3;
  cfg.selection = mscan::Selection::kRandom;
  for (std::uint64_t seed : {1u, 2u, 3u}) {
    cfg.seed = seed;
    const auto r = mscan::run_backtest(u, 0, cfg, pool);
    ASSERT_GT(r.contested_days, 0u);
    std::vector<std::vector<std::uint32_t>> picked(u.num_days());
    for (const auto& pk : r.picks) {
      EXPECT_NE(cfg.eligible[pk.ticker_idx], 0) << "ineligible ticker " << pk.ticker_idx;
      const auto& c = candidates[pk.day];
      EXPECT_NE(std::find(c.begin(), c.end(), pk.ticker_idx), c.end())
          << "day " << pk.day << " ticker " << pk.ticker_idx << " was not a candidate";
      picked[pk.day].push_back(pk.ticker_idx);
    }
    for (std::size_t d = 0; d < u.num_days(); ++d) {
      EXPECT_EQ(picked[d].size(), std::min<std::size_t>(3, candidates[d].size())) << "day " << d;
      std::sort(picked[d].begin(), picked[d].end());
      EXPECT_EQ(std::adjacent_find(picked[d].begin(), picked[d].end()), picked[d].end())
          << "repeat pick on day " << d;
    }
  }
  EXPECT_GT(all.bear_days, 0u) << "no bear days: the regime threshold went untested";
  EXPECT_EQ(all.contested_days, 0u);
}

// SPY falls the whole time, so once its 200-day MA exists every day is bear
// and an unreachable bear threshold leaves no picks on those days.
TEST(BacktestTest, BearRegimeAppliesItsThreshold) {
  const Panel p = synthetic_panel(40, 400, 3, /*spy_drift=*/-0.003);
  const auto u = p.universe();
  auto cfg = config_for(u, 1.5);
  cfg.bear_min_pv_score = 99.0;
  mscan::ThreadPool pool(2);
  const auto r = mscan::run_backtest(u, 0, cfg, pool);
  const std::size_t warmup = 200 + 1 + static_cast<std::size_t>(cfg.scoring.ma_slope_lookback);
  EXPECT_EQ(r.bear_days, u.num_days() - warmup + 1);
  for (const auto& pk : r.picks) EXPECT_LT(pk.day, warmup - 1);

  cfg.apply_regime_filter = false;
  const auto off = mscan::run_backtest(u, 0, cfg, pool);
  EXPECT_EQ(off.bear_days, 0u);
  EXPECT_GT(off.picks.size(), r.picks.size());
}

// Forward returns, excess and the per-pick stats recomputed by hand from the
// arrays, for both fill policies.
TEST(BacktestTest, ReturnsMatchAHandComputation) {
  const Panel p = synthetic_panel(30, 300, 21);
  const auto u = p.universe();
  const std::size_t D = p.days();
  for (auto fill : {mscan::FillPolicy::kCloseSameDay, mscan::FillPolicy::kOpenNextDay}) {
    auto cfg = config_for(u, 1.5);
    cfg.fill = fill;
    cfg.horizons = {1, 5};
    mscan::ThreadPool pool(2);
    const auto r = mscan::run_backtest(u, 0, cfg, pool);
    ASSERT_GT(r.picks.size(), 0u);

    auto fwd = [&](std::size_t i, std::size_t d, std::size_t h) {
      if (d + h >= D) return kNaN;
      const double entry = fill == mscan::FillPolicy::kCloseSameDay ? p.c[i * D + d] : p.o[i * D + d + 1];
      return p.c[i * D + d + h] / entry - 1.0;
    };
    for (std::size_t hi = 0; hi < cfg.horizons.size(); ++hi) {
      const auto h = static_cast<std::size_t>(cfg.horizons[hi]);
      double hits = 0, sum = 0, sum_x = 0;
      std::size_t n = 0;
      for (const auto& pk : r.picks) {
        const double want = fwd(pk.ticker_idx, pk.day, h);
        const double want_x = want - fwd(0, pk.day, h);
        if (std::isnan(want)) {
          EXPECT_TRUE(std::isnan(pk.fwd_returns[hi]));
          continue;
        }
        EXPECT_EQ(pk.fwd_returns[hi], want);
        EXPECT_EQ(pk.excess_vs_spy[hi], want_x);
        ++n;
        hits += want_x > 0;
        sum += want;
        sum_x += want_x;
      }
      const auto& st = r.stats[hi];
      EXPECT_EQ(st.horizon, cfg.horizons[hi]);
      EXPECT_EQ(st.n, n);
      EXPECT_DOUBLE_EQ(st.hit_rate, hits / static_cast<double>(n));
      EXPECT_DOUBLE_EQ(st.mean_return, sum / static_cast<double>(n));
      EXPECT_DOUBLE_EQ(st.mean_excess, sum_x / static_cast<double>(n));
      EXPECT_GE(st.max_drawdown, 0.0);
      EXPECT_LE(st.max_drawdown, 1.0);
      EXPECT_TRUE(std::isfinite(st.sharpe));
    }
  }
}

// With h = 1 the staggered portfolio is one sleeve: day t's return is the
// mean return of day t-1's picks over day t, or 0 when there were none.
TEST(BacktestTest, OneDayPortfolioSharpeAndDrawdown) {
  const Panel p = synthetic_panel(30, 300, 4);
  const auto u = p.universe();
  auto cfg = config_for(u, 1.5);
  cfg.horizons = {1};
  mscan::ThreadPool pool(2);
  const auto r = mscan::run_backtest(u, 0, cfg, pool);
  ASSERT_GT(r.picks.size(), 0u);

  const std::size_t D = p.days();
  std::vector<double> port(D - 1, 0.0);   // days 1 .. D-1
  std::vector<std::size_t> count(D, 0);
  for (const auto& pk : r.picks) ++count[pk.day];
  for (const auto& pk : r.picks) {
    if (pk.day + 1 >= D) continue;
    const std::size_t i = pk.ticker_idx;
    port[pk.day] += (p.c[i * D + pk.day + 1] / p.c[i * D + pk.day] - 1.0) / static_cast<double>(count[pk.day]);
  }
  double mean = 0;
  for (double x : port) mean += x;
  mean /= static_cast<double>(port.size());
  double var = 0;
  for (double x : port) var += (x - mean) * (x - mean);
  const double sharpe = mean / std::sqrt(var / static_cast<double>(port.size() - 1)) * std::sqrt(252.0);
  double eq = 1, peak = 1, mdd = 0;
  for (double x : port) {
    eq *= 1 + x;
    peak = std::max(peak, eq);
    mdd = std::max(mdd, 1 - eq / peak);
  }
  EXPECT_NEAR(r.stats[0].sharpe, sharpe, 1e-12);
  EXPECT_NEAR(r.stats[0].max_drawdown, mdd, 1e-12);
}

TEST(BacktestTest, RejectsBadConfig) {
  const Panel p = synthetic_panel(5, 50, 1);
  const auto u = p.universe();
  mscan::ThreadPool pool(1);
  auto cfg = config_for(u, 2.0);
  EXPECT_THROW(mscan::run_backtest(u, 99, cfg, pool), std::invalid_argument);
  cfg.end_day = 50;
  EXPECT_THROW(mscan::run_backtest(u, 0, cfg, pool), std::invalid_argument);
  cfg = config_for(u, 2.0);
  cfg.start_day = 10;
  cfg.end_day = 5;
  EXPECT_THROW(mscan::run_backtest(u, 0, cfg, pool), std::invalid_argument);
  cfg = config_for(u, 2.0);
  cfg.eligible = {1, 1};
  EXPECT_THROW(mscan::run_backtest(u, 0, cfg, pool), std::invalid_argument);
  cfg = config_for(u, 2.0);
  cfg.horizons = {0};
  EXPECT_THROW(mscan::run_backtest(u, 0, cfg, pool), std::invalid_argument);
}
