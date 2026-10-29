// Layer 1 + layer 2 scoring throughput on the real universe (engine/data/history.bin).
//
//   BM_BacktestEveryDay/T   for every ticker and every day d of the 4-year file,
//                           score_layer1 + score_layer2 on series(i, d): the shape
//                           of a backtest that rescores the universe each day.
//                           Each call recomputes indicators over the whole prefix,
//                           so total work grows with D^2 per ticker (phase 5's
//                           streaming indicators remove that).
//   BM_ProductionScan/T     the daily scan's Step 4: the last day for every
//                           ticker, on the last 206 bars (Alpaca's 300-calendar-
//                           day lookback).
//
// T = worker threads. Tickers are split across a ThreadPool with parallel_for;
// each ticker's days run in order inside its chunk.
//
// Run: engine/build/mscan_bench   (Release build recommended:
//      cmake -S engine -B engine/build-release -DCMAKE_BUILD_TYPE=Release ...)

#include <benchmark/benchmark.h>

#include <atomic>
#include <filesystem>
#include <memory>
#include <string>
#include <thread>

#include "mscan/layers.h"
#include "mscan/thread_pool.h"
#include "mscan/universe.h"

namespace {

constexpr std::size_t kProductionBars = 206;

// Mapped once and shared by every benchmark.
const mscan::Universe* universe() {
  static const std::unique_ptr<mscan::Universe> u = [] {
    const std::string path = std::string(MSCAN_ENGINE_DIR) + "/data/history.bin";
    if (!std::filesystem::exists(path)) return std::unique_ptr<mscan::Universe>();
    return std::make_unique<mscan::Universe>(mscan::Universe::open_mmap(path));
  }();
  return u.get();
}

// Keeps the optimizer from discarding results nobody reads.
double checksum(const mscan::Layer1Result& l1, const mscan::Layer2Result& l2) {
  return l1.score + l2.total;
}

void BM_BacktestEveryDay(benchmark::State& state) {
  const auto* u = universe();
  if (u == nullptr) {
    state.SkipWithError("engine/data/history.bin not found (python -m scripts.export_history --from-panel)");
    return;
  }
  const mscan::ScoringConfig cfg;
  mscan::ThreadPool pool(static_cast<std::size_t>(state.range(0)));
  const std::size_t days = u->num_days();
  std::size_t evaluations = 0;
  for (std::size_t i = 0; i < u->num_tickers(); ++i) {
    if (u->first_valid_day(i) < days) evaluations += days - u->first_valid_day(i);
  }

  for (auto _ : state) {
    std::atomic<double> sum{0.0};
    pool.parallel_for(u->num_tickers(), [&](std::size_t begin, std::size_t end) {
      double local = 0.0;   // one atomic add per chunk, not per evaluation
      for (std::size_t i = begin; i < end; ++i) {
        for (std::size_t d = u->first_valid_day(i); d < days; ++d) {
          const mscan::BarSeries s = u->series(i, d);
          local += checksum(mscan::score_layer1(s, 0, cfg), mscan::score_layer2(s, std::nullopt, cfg));
        }
      }
      sum.fetch_add(local);
    });
    benchmark::DoNotOptimize(sum.load());
  }
  state.SetItemsProcessed(static_cast<int64_t>(state.iterations() * evaluations));
  state.counters["evaluations"] = static_cast<double>(evaluations);
}

void BM_ProductionScan(benchmark::State& state) {
  const auto* u = universe();
  if (u == nullptr) {
    state.SkipWithError("engine/data/history.bin not found");
    return;
  }
  const mscan::ScoringConfig cfg;
  mscan::ThreadPool pool(static_cast<std::size_t>(state.range(0)));
  const std::size_t last = u->num_days() - 1;

  for (auto _ : state) {
    std::atomic<double> sum{0.0};
    pool.parallel_for(u->num_tickers(), [&](std::size_t begin, std::size_t end) {
      double local = 0.0;
      for (std::size_t i = begin; i < end; ++i) {
        mscan::BarSeries s = u->series(i, last);
        if (s.size() > kProductionBars) {   // keep the last 206 bars
          const std::size_t off = s.size() - kProductionBars;
          s = {s.open.subspan(off), s.high.subspan(off), s.low.subspan(off),
               s.close.subspan(off), s.volume.subspan(off)};
        }
        local += checksum(mscan::score_layer1(s, 0, cfg), mscan::score_layer2(s, std::nullopt, cfg));
      }
      sum.fetch_add(local);
    });
    benchmark::DoNotOptimize(sum.load());
  }
  state.SetItemsProcessed(static_cast<int64_t>(state.iterations() * u->num_tickers()));
}

}  // namespace

// 1, 2, 4, 8 threads, plus every hardware thread when that is another count
void thread_counts(benchmark::internal::Benchmark* b) {
  for (int t : {1, 2, 4, 8}) b->Arg(t);
  const int hw = static_cast<int>(std::thread::hardware_concurrency());
  if (hw > 0 && hw != 1 && hw != 2 && hw != 4 && hw != 8) b->Arg(hw);
}

BENCHMARK(BM_BacktestEveryDay)->Apply(thread_counts)
    ->Iterations(1)->UseRealTime()->Unit(benchmark::kSecond);
BENCHMARK(BM_ProductionScan)->Apply(thread_counts)
    ->UseRealTime()->Unit(benchmark::kMillisecond);
