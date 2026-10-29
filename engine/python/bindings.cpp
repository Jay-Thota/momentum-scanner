// pybind11 module: `import mscan_engine` from backend/scoring/scorer.py.
//
// Entry point the daily scan uses (replaces the Step 4 Python loop):
//
//   results = mscan_engine.score_l1l2_batch(
//       tickers,            # list[str], length T
//       open, high, low, close, volume,   # np.float64 arrays, shape (T, D), C-contiguous
//       cfg_dict,           # live values from backend.config
//       num_threads=8)
//   -> list[dict] in the exact shape layer1.score() / layer2.score() return today
//
// Zero-copy: the (T, D) arrays are wrapped by Universe::from_buffers, no copy.
// Python side pads tickers with < D bars with NaN at the front (left-pad) so
// every row shares the calendar.

#include <pybind11/numpy.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include "mscan/backtest.h"
#include "mscan/layers.h"
#include "mscan/thread_pool.h"
#include "mscan/universe.h"

namespace py = pybind11;
using namespace mscan;

namespace {

using F64Array = py::array_t<double, py::array::c_style | py::array::forcecast>;

ScoringConfig config_from_dict(const py::dict& d);    // TODO: read each key, fall back to defaults
py::dict to_py(const Layer1Result& r);                 // TODO: same keys as layer1.score()
py::dict to_py(const Layer2Result& r);                 // TODO: same keys as layer2.score()

py::list score_l1l2_batch(std::vector<std::string> tickers,
                          F64Array open, F64Array high, F64Array low,
                          F64Array close, F64Array volume,
                          const py::dict& cfg_dict, std::size_t num_threads) {
  // TODO: validate all five arrays are 2-D with identical shape (T, D), T == tickers.size()
  ScoringConfig cfg = config_from_dict(cfg_dict);

  std::vector<Layer1Result> l1(tickers.size());
  std::vector<Layer2Result> l2(tickers.size());

  {
    py::gil_scoped_release no_gil;   // workers never touch Python objects
    // TODO:
    //   Universe u = Universe::from_buffers(...);
    //   ThreadPool pool(num_threads);
    //   pool.parallel_for(T, [&](size_t b, size_t e) {
    //     for (size_t i = b; i < e; ++i) {
    //       BarSeries s = u.series(i, u.num_days() - 1);
    //       l1[i] = score_layer1(s, /*block_trades=*/0, cfg);
    //       l2[i] = score_layer2(s, std::nullopt, cfg);
    //     }
    //   });
  }

  py::list out;
  for (std::size_t i = 0; i < tickers.size(); ++i) {
    py::dict row;
    row["l1"] = to_py(l1[i]);
    row["l2"] = to_py(l2[i]);
    out.append(row);
  }
  return out;
}

}  // namespace

PYBIND11_MODULE(mscan_engine, m) {
  m.doc() = "C++ scoring + backtest engine for momentum-scanner";
  m.def("score_l1l2_batch", &score_l1l2_batch,
        py::arg("tickers"), py::arg("open"), py::arg("high"), py::arg("low"),
        py::arg("close"), py::arg("volume"), py::arg("cfg"),
        py::arg("num_threads") = 8);
  // TODO: m.def("run_backtest", ...) taking a path to the mmapped history file
}
