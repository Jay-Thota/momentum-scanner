#pragma once
// Indicator kernels used by layers 1 and 2.
//
// Batch kernels: write a full output series into a caller-provided span.
// Used by the layer scorers and for parity tests against the Python versions in
// layer2_structure.py and fetcher_price.py. NaN for warm-up bars, same as pandas
// rolling(). StreamingIndicators (mscan/streaming.h) produces the same values
// one bar at a time for the backtester.
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

// Streaming versions (O(1) per new bar) live in mscan/streaming.h.

}  // namespace mscan::ind
