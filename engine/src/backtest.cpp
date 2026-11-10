#include "mscan/backtest.h"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <limits>
#include <random>
#include <stdexcept>
#include <string>

#include "mscan/streaming.h"

namespace mscan {
namespace {

constexpr double kNaN = std::numeric_limits<double>::quiet_NaN();
constexpr std::size_t kL0VolumeWindow = 20;   // scorer._L0_VOLUME_WINDOW
constexpr std::size_t kRegimeMa = 200;        // regime_filter: close.rolling(200)
constexpr double kTradingDays = 252.0;

void validate(const Universe& u, std::size_t spy_idx, const BacktestConfig& cfg) {
  auto fail = [](const std::string& what) { throw std::invalid_argument("run_backtest: " + what); };
  if (spy_idx >= u.num_tickers()) fail("spy_idx out of range");
  if (cfg.end_day >= u.num_days()) fail("end_day past the last day");
  if (cfg.start_day > cfg.end_day) fail("start_day after end_day");
  if (cfg.top_n < 1) fail("top_n must be >= 1");
  for (int h : cfg.horizons) {
    if (h < 1) fail("horizons must be >= 1");
  }
  if (!cfg.eligible.empty() && cfg.eligible.size() != u.num_tickers()) {
    fail("eligible must be empty or have one entry per ticker");
  }
}

// Step 2: >= 20 bars, close >= MIN_PRICE, mean of the last 20 volumes
// (NaN skipped, like pandas .mean()) >= MIN_AVG_VOLUME
bool passes_l0(const BarSeries& s, const BacktestConfig& cfg) {
  const std::size_t n = s.size();
  if (n < kL0VolumeWindow) return false;
  double sum = 0.0;
  std::size_t count = 0;
  for (std::size_t k = n - kL0VolumeWindow; k < n; ++k) {
    if (!std::isnan(s.volume[k])) {
      sum += s.volume[k];
      ++count;
    }
  }
  const double avg = count == 0 ? kNaN : sum / static_cast<double>(count);
  return s.close[n - 1] >= cfg.min_price && avg >= cfg.min_avg_volume;
}

// Selection phase: the PV score of every (ticker, day) in [start_day,
// end_day], NaN when the ticker is ineligible, has no data yet, or fails the
// gate. Stored ticker-major (scores[i * days + k]) so each worker writes one
// contiguous block and never shares a cache line with another worker, apart
// from the edges of its block.
std::vector<double> score_all(const Universe& u, const BacktestConfig& cfg,
                              const std::vector<std::uint8_t>& eligible, ThreadPool& pool,
                              std::size_t& evaluations) {
  const std::size_t days = cfg.end_day - cfg.start_day + 1;
  std::vector<double> scores(u.num_tickers() * days, kNaN);
  std::vector<std::size_t> evals(u.num_tickers(), 0);

  pool.parallel_for(u.num_tickers(), [&](std::size_t begin, std::size_t end) {
    for (std::size_t i = begin; i < end; ++i) {
      if (!eligible[i]) continue;
      // About 6 KB on this worker's stack, reset for every ticker
      ind::StreamingIndicators state(cfg.scoring.roc_period, cfg.scoring.obv_lookback_days);
      for (std::size_t d = u.first_valid_day(i); d <= cfg.end_day; ++d) {
        // Only day d's bar enters the state, and only series(i, d) is scored
        const BarSeries s = u.series(i, d);
        const std::size_t last = s.size() - 1;
        state.update(s.open[last], s.high[last], s.low[last], s.close[last], s.volume[last]);
        if (d < cfg.start_day || !passes_l0(s, cfg)) continue;
        const IndicatorSnapshot snap = snapshot(state, cfg.scoring);
        const double pv = score_layer1(s, 0, snap, cfg.scoring).score +
                          score_layer2(s, std::nullopt, snap, cfg.scoring).total;
        scores[i * days + (d - cfg.start_day)] = pv;
        ++evals[i];
      }
    }
  });
  evaluations = 0;
  for (std::size_t e : evals) evaluations += e;
  return scores;
}

// regime_filter.get_regime on SPY as of each day: bull when the close is
// above its 200-day MA and the MA's slope over `lookback` sessions is >=
// slope_min. Bull by default when history is too short, as in Python.
std::vector<bool> bear_days(const Universe& u, std::size_t spy_idx, const BacktestConfig& cfg) {
  const std::size_t days = cfg.end_day - cfg.start_day + 1;
  const auto lookback = static_cast<std::size_t>(cfg.scoring.ma_slope_lookback);
  std::vector<bool> bear(days, false);
  ind::StreamingIndicators spy(cfg.scoring.roc_period, cfg.scoring.obv_lookback_days);
  for (std::size_t d = u.first_valid_day(spy_idx); d <= cfg.end_day; ++d) {
    const BarSeries s = u.series(spy_idx, d);
    const std::size_t last = s.size() - 1;
    spy.update(s.open[last], s.high[last], s.low[last], s.close[last], s.volume[last]);
    if (d < cfg.start_day || s.size() < kRegimeMa + 1 + lookback) continue;
    const double ma = spy.sma200(), ma_old = spy.sma200_ago(lookback);
    const double slope = ma_old != 0 ? (ma - ma_old) / ma_old : 0.0;
    const bool bull = s.close[last] > ma && slope >= cfg.regime_slope_min;
    bear[d - cfg.start_day] = !bull;
  }
  return bear;
}

// Uniform integer in [0, n). Not std::uniform_int_distribution: its algorithm
// is up to the standard library, so libc++ (macOS) and libstdc++ (Linux CI)
// would draw different picks from the same seed. mt19937_64's output is fixed
// by the standard; rejecting the top partial block of 2^64 % n values keeps
// the draw exactly uniform.
std::size_t uniform_below(std::mt19937_64& rng, std::size_t n) {
  const std::uint64_t range = n;
  const std::uint64_t reject_from = std::numeric_limits<std::uint64_t>::max() -
                                    std::numeric_limits<std::uint64_t>::max() % range;
  std::uint64_t x;
  do {
    x = rng();
  } while (x >= reject_from);
  return static_cast<std::size_t>(x % range);
}

// Ticker i's open or close on day d; NaN before its first bar or past the end
double open_at(const Universe& u, std::size_t i, std::size_t d) {
  if (d >= u.num_days()) return kNaN;
  const BarSeries s = u.series(i, d);
  return s.empty() ? kNaN : s.open.back();
}

double close_at(const Universe& u, std::size_t i, std::size_t d) {
  if (d >= u.num_days()) return kNaN;
  const BarSeries s = u.series(i, d);
  return s.empty() ? kNaN : s.close.back();
}

double entry_price(const Universe& u, std::size_t i, std::size_t d, FillPolicy fill) {
  return fill == FillPolicy::kCloseSameDay ? close_at(u, i, d) : open_at(u, i, d + 1);
}

// Close of d + h over the entry price; NaN past the end of data
double forward_return(const Universe& u, std::size_t i, std::size_t d, int h, FillPolicy fill) {
  return close_at(u, i, d + static_cast<std::size_t>(h)) / entry_price(u, i, d, fill) - 1.0;
}

// Return of a pick made on day d over held day t (d < t <= d + h): the first
// day from the entry price, then close to close. They compound to the
// forward return.
double daily_return(const Universe& u, std::size_t i, std::size_t d, std::size_t t,
                    FillPolicy fill) {
  const double prev = t == d + 1 ? entry_price(u, i, d, fill) : close_at(u, i, t - 1);
  return close_at(u, i, t) / prev - 1.0;
}

HorizonStats horizon_stats(const Universe& u, const BacktestConfig& cfg,
                           const std::vector<Pick>& picks, std::size_t hi) {
  const int h = cfg.horizons[hi];
  HorizonStats st{h, kNaN, kNaN, kNaN, kNaN, kNaN, 0};

  double hits = 0, sum_ret = 0, sum_exc = 0;
  for (const Pick& p : picks) {
    const double r = p.fwd_returns[hi], x = p.excess_vs_spy[hi];
    if (std::isnan(r) || std::isnan(x)) continue;
    ++st.n;
    hits += x > 0;
    sum_ret += r;
    sum_exc += x;
  }
  if (st.n > 0) {
    const auto n = static_cast<double>(st.n);
    st.hit_rate = hits / n;
    st.mean_return = sum_ret / n;
    st.mean_excess = sum_exc / n;
  }

  // Staggered portfolio. Picks are grouped by day (they are generated in day
  // order); cohort c of day d is live on days d+1 .. d+h.
  const std::size_t first_t = cfg.start_day + 1;
  const std::size_t last_t = std::min(cfg.end_day + static_cast<std::size_t>(h), u.num_days() - 1);
  if (last_t < first_t) return st;
  std::vector<double> port(last_t - first_t + 1, 0.0);
  for (std::size_t a = 0; a < picks.size();) {
    std::size_t b = a;
    while (b < picks.size() && picks[b].day == picks[a].day) ++b;
    const std::size_t d = picks[a].day;
    const double weight = 1.0 / (static_cast<double>(h) * static_cast<double>(b - a));
    for (std::size_t t = d + 1; t <= std::min(d + static_cast<std::size_t>(h), last_t); ++t) {
      for (std::size_t k = a; k < b; ++k) {
        const double r = daily_return(u, picks[k].ticker_idx, d, t, cfg.fill);
        if (!std::isnan(r)) port[t - first_t] += weight * r;   // missing bar: flat that day
      }
    }
    a = b;
  }

  double mean = 0;
  for (double r : port) mean += r;
  mean /= static_cast<double>(port.size());
  double var = 0;
  for (double r : port) var += (r - mean) * (r - mean);
  if (port.size() > 1) {
    const double sd = std::sqrt(var / static_cast<double>(port.size() - 1));
    st.sharpe = sd > 0 ? mean / sd * std::sqrt(kTradingDays) : kNaN;
  }
  double equity = 1.0, peak = 1.0, mdd = 0.0;
  for (double r : port) {
    equity *= 1.0 + r;
    peak = std::max(peak, equity);
    mdd = std::max(mdd, 1.0 - equity / peak);
  }
  st.max_drawdown = mdd;
  return st;
}

}  // namespace

BacktestResult run_backtest(const Universe& u, std::size_t spy_idx, const BacktestConfig& cfg,
                            ThreadPool& pool) {
  const auto t0 = std::chrono::steady_clock::now();
  validate(u, spy_idx, cfg);
  std::vector<std::uint8_t> eligible = cfg.eligible;
  if (eligible.empty()) {
    eligible.assign(u.num_tickers(), 1);
    eligible[spy_idx] = 0;
  }

  BacktestResult result;
  const std::size_t days = cfg.end_day - cfg.start_day + 1;
  const std::vector<double> scores = score_all(u, cfg, eligible, pool, result.evaluations);
  const std::vector<bool> bear = cfg.apply_regime_filter ? bear_days(u, spy_idx, cfg)
                                                         : std::vector<bool>(days, false);

  // Picks: per day, top_n of the candidates at or above that day's threshold.
  // kTopScore: highest scores, ties to the lower ticker index (alphabetical
  // within the equities, like the scanner's stable sort). kRandom: one rng
  // seeded once and consumed in day order, so picks depend only on the seed
  // and the candidates, not on the thread count. kRandomUniverse: same draw,
  // but every L0 pass is a candidate (scores are NaN for the rest).
  std::mt19937_64 rng(cfg.seed);
  std::vector<std::pair<double, std::uint32_t>> candidates;
  for (std::size_t k = 0; k < days; ++k) {
    const double threshold = cfg.selection == Selection::kRandomUniverse
                                 ? -std::numeric_limits<double>::infinity()
                             : bear[k] ? cfg.bear_min_pv_score
                                       : cfg.min_pv_score;
    result.bear_days += bear[k];
    candidates.clear();
    for (std::size_t i = 0; i < u.num_tickers(); ++i) {
      const double s = scores[i * days + k];
      if (s >= threshold) candidates.emplace_back(s, static_cast<std::uint32_t>(i));   // NaN fails
    }
    const std::size_t take = std::min(candidates.size(), static_cast<std::size_t>(cfg.top_n));
    result.contested_days += candidates.size() > take;
    if (cfg.selection != Selection::kTopScore) {
      // Partial Fisher-Yates: after step c, candidates[0..c] is a uniform
      // random sample without replacement
      for (std::size_t c = 0; c < take; ++c) {
        std::swap(candidates[c], candidates[c + uniform_below(rng, candidates.size() - c)]);
      }
    } else {
      std::partial_sort(candidates.begin(), candidates.begin() + static_cast<std::ptrdiff_t>(take),
                        candidates.end(), [](const auto& a, const auto& b) {
                          return a.first != b.first ? a.first > b.first : a.second < b.second;
                        });
    }
    for (std::size_t c = 0; c < take; ++c) {
      Pick p;
      p.day = cfg.start_day + k;
      p.date = u.date(p.day);
      p.ticker_idx = candidates[c].second;
      p.score = candidates[c].first;
      result.picks.push_back(std::move(p));
    }
  }

  // Evaluation phase: forward returns read past the pick day by design
  for (Pick& p : result.picks) {
    for (int h : cfg.horizons) {
      const double r = forward_return(u, p.ticker_idx, p.day, h, cfg.fill);
      p.fwd_returns.push_back(r);
      p.excess_vs_spy.push_back(r - forward_return(u, spy_idx, p.day, h, cfg.fill));
    }
  }
  for (std::size_t hi = 0; hi < cfg.horizons.size(); ++hi) {
    result.stats.push_back(horizon_stats(u, cfg, result.picks, hi));
  }

  result.elapsed_ms =
      std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0).count();
  return result;
}

}  // namespace mscan
