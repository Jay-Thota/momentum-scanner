// pybind11 module: `import mscan_engine`, used by backend/scoring/cpp_engine.py.
//
// Entry point the daily scan uses (replaces the Step 4 Python loop):
//
//   results = mscan_engine.score_l1l2_batch(
//       tickers,            # list[str], length T
//       open, high, low, close, volume,   # np.float64 arrays, shape (T, D), C-contiguous
//       cfg_dict,           # live values from backend.config (UPPER_CASE keys)
//       num_threads=1,      # 1 scores on the calling thread; more uses a ThreadPool
//       dates=None)         # optional int64 (T, D) YYYYMMDD, 0 for padding
//   -> list[{"l1": dict, "l2": dict}] in the exact shape layer1.score() /
//      layer2.score() return today (sector ETF empty, rs_rank None,
//      block_trades 0, as in Step 4)
//
// Backtest (scripts/run_backtest.py):
//
//   result = mscan_engine.run_backtest(
//       path,               # engine/data/history.bin, mmapped by the engine
//       cfg_dict,           # backend.config values, as above, plus MIN_PRICE,
//                           # MIN_AVG_VOLUME and SECTOR_SLOPE_MIN
//       start=None, end=None,   # YYYYMMDD ints, inclusive; None = first / last day
//       exclude=[...],      # tickers never picked (SPY is always excluded)
//       ...)                # top_n, thresholds, horizons, fill, regime_filter, num_threads
//   -> {"picks": [...], "stats": [...], "elapsed_ms", "evaluations", "bear_days", ...}
//
// Zero-copy: the (T, D) arrays are wrapped by Universe::from_buffers, no copy.
// Each row holds one ticker's bars, left-padded with NaN to length D. Rows are
// right-aligned bar sequences rather than one shared calendar, so a ticker
// with a missing day scores on exactly the bars Python sees; `dates` carries
// each cell's date so a breakout reports the same date string as Python.

#include <pybind11/numpy.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <cmath>
#include <cstdio>
#include <optional>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#include "mscan/backtest.h"
#include "mscan/layers.h"
#include "mscan/thread_pool.h"
#include "mscan/universe.h"

namespace py = pybind11;
using namespace mscan;

namespace {

using F64Array = py::array_t<double, py::array::c_style | py::array::forcecast>;
using I64Array = py::array_t<int64_t, py::array::c_style | py::array::forcecast>;

template <class T>
void read_key(const py::dict& d, const char* key, T& field) {
  if (d.contains(key)) field = d[key].cast<T>();   // missing keys keep the config.py default
}

ScoringConfig config_from_dict(const py::dict& d) {
  ScoringConfig c;
  read_key(d, "OBV_LOOKBACK_DAYS", c.obv_lookback_days);
  read_key(d, "VOLUME_SURGE_RATIO", c.volume_surge_ratio);
  read_key(d, "RVOL_THRESHOLD", c.rvol_threshold);
  read_key(d, "UP_DOWN_VOL_RATIO_MIN", c.up_down_vol_ratio_min);
  read_key(d, "LAYER1_MIN_SIGNALS", c.layer1_min_signals);
  read_key(d, "ADX_MIN", c.adx_min);
  read_key(d, "ROC_PERIOD", c.roc_period);
  read_key(d, "MA_SLOPE_LOOKBACK", c.ma_slope_lookback);
  read_key(d, "GOLDEN_CROSS_LOOKBACK", c.golden_cross_lookback);
  read_key(d, "BREAKOUT_PCT_MIN", c.breakout_pct_min);
  read_key(d, "BREAKOUT_VOLUME_RATIO", c.breakout_volume_ratio);
  read_key(d, "BREAKOUT_HOLD_SESSIONS", c.breakout_hold_sessions);
  read_key(d, "DISTRIBUTION_DAY_MAX", c.distribution_day_max);
  read_key(d, "UPPER_RANGE_CLOSE_MIN", c.upper_range_close_min);
  read_key(d, "MA_BONUS_PER_LEVEL", c.ma_bonus_per_level);
  read_key(d, "L2_MOMENTUM_A_SCORE", c.l2_momentum_a_score);
  read_key(d, "L2_MOMENTUM_B_SCORE", c.l2_momentum_b_score);
  return c;
}

// Python returns None for these when the indicator is NaN
py::object none_if_nan(double v) { return std::isnan(v) ? py::none() : py::cast(v); }

std::string format_date(int64_t yyyymmdd) {
  char buf[16];
  std::snprintf(buf, sizeof buf, "%04lld-%02lld-%02lld", static_cast<long long>(yyyymmdd / 10000),
                static_cast<long long>(yyyymmdd / 100 % 100), static_cast<long long>(yyyymmdd % 100));
  return buf;
}

// Same keys, order and value types as layer1_volume.score()
py::dict to_py(const Layer1Result& r) {
  py::dict out;
  out["score"] = r.score;
  py::dict s;
  if (r.valid) {
    s["A"] = r.A;
    s["B"] = r.B;
    s["C"] = r.C;
    s["D"] = r.D;
    s["E"] = r.E;
    s["vol_ratio"] = r.vol_ratio;
    s["rvol"] = r.vol_ratio;   // Python reports the same value under both keys
    s["up_down_ratio"] = r.up_down_ratio;
    s["block_trades"] = r.block_trades;
    s["signals_fired"] = r.signals_fired;
  }
  out["signals"] = s;
  return out;
}

// Same keys, order and value types as layer2_structure.score()
py::dict to_py(const Layer2Result& r, const py::object& breakout_date) {
  py::dict out;
  out["archetype"] = r.archetype ? py::cast(*r.archetype) : py::none();
  out["archetype_score"] = r.archetype_score;
  out["momentum_score"] = r.momentum_score;
  out["total"] = r.total;
  py::dict s;
  if (r.valid) {
    py::dict arch_info;
    if (r.breakout_day >= 0) {
      arch_info["breakout_date"] = breakout_date;
      arch_info["pct_chg"] = r.breakout_pct;
      arch_info["vol_ratio"] = r.breakout_vol_ratio;
    }
    s["arch_info"] = arch_info;
    s["A"] = r.mom_A;
    s["B"] = r.mom_B;
    s["C_pts"] = r.ma_pts;
    s["roc10"] = none_if_nan(r.roc10);
    s["adx"] = none_if_nan(r.adx);
    s["di_p"] = none_if_nan(r.di_p);
    s["di_n"] = none_if_nan(r.di_n);
    s["dist_days"] = r.dist_days;
    s["close_pct"] = r.close_pct;
  }
  out["signals"] = s;
  return out;
}

void require_shape(const py::array& a, const char* name, py::ssize_t t, py::ssize_t d) {
  if (a.ndim() != 2 || a.shape(0) != t || a.shape(1) != d) {
    throw std::invalid_argument(std::string(name) + " must have shape (T, D) matching open and tickers");
  }
}

py::list score_l1l2_batch(std::vector<std::string> tickers,
                          F64Array open, F64Array high, F64Array low,
                          F64Array close, F64Array volume,
                          const py::dict& cfg_dict, std::size_t num_threads,
                          std::optional<I64Array> dates) {
  if (num_threads < 1) throw std::invalid_argument("num_threads must be >= 1");
  if (open.ndim() != 2) throw std::invalid_argument("open must be a 2-D (T, D) array");
  const py::ssize_t t = open.shape(0), d = open.shape(1);
  if (static_cast<std::size_t>(t) != tickers.size()) {
    throw std::invalid_argument("len(tickers) must equal the first dimension of the arrays");
  }
  require_shape(high, "high", t, d);
  require_shape(low, "low", t, d);
  require_shape(close, "close", t, d);
  require_shape(volume, "volume", t, d);
  if (dates) require_shape(*dates, "dates", t, d);

  const ScoringConfig cfg = config_from_dict(cfg_dict);
  const auto num = static_cast<std::size_t>(t), days = static_cast<std::size_t>(d);
  std::vector<Layer1Result> l1(num);
  std::vector<Layer2Result> l2(num);
  std::vector<std::size_t> first(num);

  // Raw pointers are taken while holding the GIL; the arrays stay alive as
  // function arguments for the whole call.
  const double *o = open.data(), *h = high.data(), *lo = low.data(), *c = close.data(), *v = volume.data();
  {
    py::gil_scoped_release no_gil;   // nothing below touches a Python object
    // The calendar is unused here (dates come from `dates`), so it is zeros.
    Universe u = Universe::from_buffers(std::move(tickers), std::vector<int32_t>(days, 0), o, h, lo, c, v);
    // Each index writes only its own slots of first/l1/l2, so chunks never
    // share data and need no locking.
    auto score_rows = [&](std::size_t begin, std::size_t end) {
      for (std::size_t i = begin; i < end; ++i) {
        first[i] = u.first_valid_day(i);
        const BarSeries s = days == 0 ? BarSeries{} : u.series(i, days - 1);
        l1[i] = score_layer1(s, /*block_trades=*/0, cfg);
        l2[i] = score_layer2(s, std::nullopt, cfg);
      }
    };
    if (num_threads == 1) {
      score_rows(0, num);
    } else {
      ThreadPool pool(num_threads);
      pool.parallel_for(num, score_rows);
    }
  }

  py::list out;
  for (std::size_t i = 0; i < num; ++i) {
    py::object bo_date = py::none();
    if (l2[i].breakout_day >= 0 && dates) {
      const std::size_t col = first[i] + static_cast<std::size_t>(l2[i].breakout_day);
      bo_date = py::cast(format_date(dates->at(static_cast<py::ssize_t>(i), static_cast<py::ssize_t>(col))));
    }
    py::dict row;
    row["l1"] = to_py(l1[i]);
    row["l2"] = to_py(l2[i], bo_date);
    out.append(row);
  }
  return out;
}

// Index of the first day >= date (or of the last day <= date when `last`)
std::size_t day_index(const Universe& u, int32_t date, bool last) {
  if (last) {
    for (std::size_t d = u.num_days(); d-- > 0;) {
      if (u.date(d) <= date) return d;
    }
    throw std::invalid_argument("end is before the first day in the file");
  }
  for (std::size_t d = 0; d < u.num_days(); ++d) {
    if (u.date(d) >= date) return d;
  }
  throw std::invalid_argument("start is after the last day in the file");
}

py::dict run_backtest_py(const std::string& path, const py::dict& cfg_dict,
                         std::optional<int32_t> start, std::optional<int32_t> end,
                         const std::vector<std::string>& exclude, const std::string& spy,
                         int top_n, double min_pv_score, double bear_min_pv_score,
                         std::vector<int> horizons, const std::string& fill, bool regime_filter,
                         std::size_t num_threads, const std::string& selection,
                         std::uint64_t seed) {
  // Mapping the file and building the config only read Python objects up
  // front; the backtest itself runs without the GIL.
  Universe u = Universe::open_mmap(path);
  BacktestConfig cfg;
  cfg.scoring = config_from_dict(cfg_dict);
  read_key(cfg_dict, "MIN_PRICE", cfg.min_price);
  read_key(cfg_dict, "MIN_AVG_VOLUME", cfg.min_avg_volume);
  read_key(cfg_dict, "SECTOR_SLOPE_MIN", cfg.regime_slope_min);
  cfg.start_day = start ? day_index(u, *start, false) : 0;
  cfg.end_day = end ? day_index(u, *end, true) : u.num_days() - 1;
  cfg.top_n = top_n;
  cfg.min_pv_score = min_pv_score;
  cfg.bear_min_pv_score = bear_min_pv_score;
  cfg.horizons = std::move(horizons);
  if (fill == "close_same_day") {
    cfg.fill = FillPolicy::kCloseSameDay;
  } else if (fill == "open_next_day") {
    cfg.fill = FillPolicy::kOpenNextDay;
  } else {
    throw std::invalid_argument("fill must be 'open_next_day' or 'close_same_day'");
  }
  cfg.apply_regime_filter = regime_filter;
  if (selection == "top_score") {
    cfg.selection = Selection::kTopScore;
  } else if (selection == "random") {
    cfg.selection = Selection::kRandom;
  } else if (selection == "random_universe") {
    cfg.selection = Selection::kRandomUniverse;
  } else {
    throw std::invalid_argument("selection must be 'top_score', 'random' or 'random_universe'");
  }
  cfg.seed = seed;

  std::optional<std::size_t> spy_idx;
  cfg.eligible.assign(u.num_tickers(), 1);
  for (std::size_t i = 0; i < u.num_tickers(); ++i) {
    if (u.ticker(i) == spy) spy_idx = i;
    for (const auto& t : exclude) {
      if (u.ticker(i) == t) cfg.eligible[i] = 0;
    }
  }
  if (!spy_idx) throw std::invalid_argument(spy + " is not in " + path);
  cfg.eligible[*spy_idx] = 0;

  BacktestResult r;
  {
    py::gil_scoped_release no_gil;
    ThreadPool pool(num_threads == 0 ? std::thread::hardware_concurrency() : num_threads);
    r = run_backtest(u, *spy_idx, cfg, pool);
  }

  auto nan_list = [](const std::vector<double>& v) {
    py::list out;
    for (double x : v) out.append(none_if_nan(x));
    return out;
  };
  py::list picks;
  for (const Pick& p : r.picks) {
    py::dict d;
    d["date"] = format_date(p.date);
    d["ticker"] = u.ticker(p.ticker_idx);
    d["score"] = p.score;
    d["fwd_returns"] = nan_list(p.fwd_returns);
    d["excess_vs_spy"] = nan_list(p.excess_vs_spy);
    picks.append(d);
  }
  py::list stats;
  for (const HorizonStats& h : r.stats) {
    py::dict d;
    d["horizon"] = h.horizon;
    d["n"] = h.n;
    d["hit_rate"] = none_if_nan(h.hit_rate);
    d["mean_return"] = none_if_nan(h.mean_return);
    d["mean_excess"] = none_if_nan(h.mean_excess);
    d["sharpe"] = none_if_nan(h.sharpe);
    d["max_drawdown"] = none_if_nan(h.max_drawdown);
    stats.append(d);
  }
  py::dict out;
  out["picks"] = picks;
  out["stats"] = stats;
  out["elapsed_ms"] = r.elapsed_ms;
  out["evaluations"] = r.evaluations;
  out["bear_days"] = r.bear_days;
  out["contested_days"] = r.contested_days;
  out["start"] = format_date(u.date(cfg.start_day));
  out["end"] = format_date(u.date(cfg.end_day));
  out["num_days"] = cfg.end_day - cfg.start_day + 1;
  out["num_tickers"] = u.num_tickers();
  return out;
}

}  // namespace

PYBIND11_MODULE(mscan_engine, m) {
  m.doc() = "C++ scoring + backtest engine for momentum-scanner";
  m.def("score_l1l2_batch", &score_l1l2_batch,
        py::arg("tickers"), py::arg("open"), py::arg("high"), py::arg("low"),
        py::arg("close"), py::arg("volume"), py::arg("cfg"),
        py::arg("num_threads") = 1, py::arg("dates") = py::none(),
        "Score layers 1 and 2 for every row of the (T, D) arrays. Returns one "
        "{'l1': ..., 'l2': ...} dict per ticker, shaped like the Python layers.");
  m.def("run_backtest", &run_backtest_py,
        py::arg("path"), py::arg("cfg"), py::kw_only(),
        py::arg("start") = py::none(), py::arg("end") = py::none(),
        py::arg("exclude") = std::vector<std::string>{}, py::arg("spy") = "SPY",
        py::arg("top_n") = 10, py::arg("min_pv_score") = 2.0,
        py::arg("bear_min_pv_score") = 2.5,
        py::arg("horizons") = std::vector<int>{5, 10, 20},
        py::arg("fill") = "open_next_day", py::arg("regime_filter") = true,
        py::arg("num_threads") = 0, py::arg("selection") = "top_score",
        py::arg("seed") = 0,
        "Backtest the PV-only score (layers 1 + 2) over the mmapped history file. "
        "num_threads=0 uses every hardware thread. selection='random' draws top_n "
        "of each day's candidates at random (a baseline), reproducibly from seed; "
        "'random_universe' draws from every ticker that passes the L0 gate, ignoring "
        "the score threshold.");
}
