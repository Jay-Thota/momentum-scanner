#pragma once
// Indicator kernels used by layers 1 and 2.
//
// Two flavors:
//   1. Batch kernels: write a full output series into a caller-provided span.
//      Used for parity tests against the Python versions in layer2_structure.py
//      and fetcher_price.py. NaN for warm-up bars, same as pandas rolling().
//   2. Streaming state (RollingWindow, StreamingIndicators): O(1) update per new
//      bar. The backtester uses these so day d+1 does not recompute day d's work.
//
// IMPORTANT for parity: the Python ADX smooths TR, +DM, -DM and DX with a plain
// rolling MEAN, not Wilder's smoothing. Match the Python first; switching to
// Wilder later is a deliberate, documented change.
//
// Rule: no heap allocation inside any kernel. Callers own the buffers.

#include <cstddef>
#include <span>

#include "mscan/universe.h"

namespace mscan::ind {

// ---- batch kernels (out.size() must equal input size) ----------------------
void sma(std::span<const double> x, int period, std::span<double> out);
void roc(std::span<const double> close, int period, std::span<double> out);   // percent
void obv(std::span<const double> close, std::span<const double> volume,
         std::span<double> out);

struct AdxOut {
  std::span<double> adx, plus_di, minus_di;
};
void adx(const BarSeries& bars, int period, AdxOut out);

// ---- streaming --------------------------------------------------------------

// Fixed-capacity ring buffer keeping a running sum (and optional running max
// via a monotonic deque, needed for "OBV at 20d high" and "ETF at 20d high").
template <int N>
class RollingWindow {
 public:
  void push(double v);          // O(1) amortized
  bool full() const;
  double sum() const;
  double mean() const;          // NaN until full
  double max() const;           // NaN until full
  double oldest() const;        // value that will fall out next
 private:
  double buf_[N]{};
  int head_ = 0, count_ = 0;
  double sum_ = 0.0;
  // TODO: monotonic deque of (index, value) for max()
};

// All per-ticker state needed to score L1 + L2 on the newest bar.
// Feed bars oldest -> newest with update(); read the latest values any time.
class StreamingIndicators {
 public:
  void update(double open, double high, double low, double close, double volume);

  bool ready() const;           // enough history for SMA-200 and ADX
  double sma20() const;
  double sma50() const;
  double sma200() const;
  double roc10() const;
  double adx14() const;
  double plus_di() const;
  double minus_di() const;
  double obv() const;
  double obv_20d_high() const;
  double avg_vol_prev20() const;   // 20 bars BEFORE today (matches layer1)

 private:
  // TODO: RollingWindow members, previous bar, running OBV, etc.
};

}  // namespace mscan::ind
