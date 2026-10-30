#pragma once
// Streaming indicators: O(1) update per new bar.
//
// Feeding a ticker's bars oldest -> newest through update() leaves the same
// values the batch kernels write at the newest index, bit for bit, as long as
// both start from the same first bar. The backtester relies on that: it scores
// day d from this state instead of recomputing every indicator over the whole
// prefix, which makes a full backtest linear in days instead of quadratic.
//
// Everything lives in fixed-size std::arrays inside the object (about 6 KB),
// so update() never touches the heap and a StreamingIndicators can sit on a
// worker thread's stack.

#include <array>
#include <cmath>
#include <cstddef>
#include <limits>

#include "mscan/detail/pandas_rolling_mean.h"

namespace mscan::ind {

// The last N values pushed. ago(0) is the newest, ago(k) the one k pushes
// before it; NaN when fewer than k + 1 values have been pushed.
template <std::size_t N>
class Ring {
 public:
  void push(double v) {
    head_ = (head_ + 1) % N;
    buf_[head_] = v;
    if (count_ < N) ++count_;
  }
  double ago(std::size_t k) const {
    return k < count_ ? buf_[(head_ + N - k) % N] : std::numeric_limits<double>::quiet_NaN();
  }

 private:
  std::array<double, N> buf_{};
  std::size_t head_ = N - 1;   // first push lands on slot 0
  std::size_t count_ = 0;
};

// Max of the last `window` values, skipping NaN (pandas .max()); NaN when all
// of them are NaN. A monotonic deque: values are kept in decreasing order, so
// the front is the max. Each value enters and leaves the deque once, so push
// is O(1) amortized. The deque is a ring of N slots; it never holds more than
// `window` entries because older ones are evicted by index.
template <std::size_t N>
class RollingMax {
 public:
  explicit RollingMax(std::size_t window) : window_(window) {}

  void push(double v) {
    const std::size_t idx = next_++;
    // Drop the front once it falls out of the window [idx - window + 1, idx]
    if (size_ > 0 && at(0).idx + window_ <= idx) pop_front();
    if (std::isnan(v)) return;   // NaN: takes a slot in the window, never the max
    // Anything smaller than v can never be the max again while v is in the window
    while (size_ > 0 && at(size_ - 1).value <= v) --size_;
    slots_[(front_ + size_) % N] = {idx, v};
    ++size_;
  }
  double max() const {
    return size_ > 0 ? at(0).value : std::numeric_limits<double>::quiet_NaN();
  }

 private:
  struct Entry {
    std::size_t idx;
    double value;
  };
  const Entry& at(std::size_t k) const { return slots_[(front_ + k) % N]; }
  void pop_front() {
    front_ = (front_ + 1) % N;
    --size_;
  }

  std::array<Entry, N> slots_{};
  std::size_t window_;
  std::size_t front_ = 0, size_ = 0, next_ = 0;
};

// All per-ticker indicator state layers 1 and 2 need on the newest bar.
// Periods mirror the batch callers: SMA 20/50/200 and ADX 14 are fixed in
// layer2_structure.py; the ROC period and OBV window come from ScoringConfig.
class StreamingIndicators {
 public:
  static constexpr std::size_t kAdxPeriod = 14;
  static constexpr std::size_t kMaxRocPeriod = 255;
  static constexpr std::size_t kMaxObvWindow = 64;
  static constexpr std::size_t kMaxMaLag = 63;   // ma_slope / golden_cross lookbacks

  // Throws std::invalid_argument when a period is outside the fixed buffers.
  StreamingIndicators(int roc_period, int obv_window);

  void update(double open, double high, double low, double close, double volume);

  std::size_t count() const { return count_; }   // bars seen so far

  // Values at the newest bar, identical to the batch kernels' last output.
  double sma20() const { return sma20_.value(); }
  double sma50() const { return sma50_.value(); }
  double sma200() const { return sma200_.value(); }
  double sma50_ago(std::size_t k) const { return ma50_hist_.ago(k); }    // k <= kMaxMaLag
  double sma200_ago(std::size_t k) const { return ma200_hist_.ago(k); }
  double roc() const { return roc_; }
  double adx() const { return adx_.value(); }
  double plus_di() const { return plus_di_; }
  double minus_di() const { return minus_di_; }
  double obv() const { return obv_; }                 // NaN on a bar whose flow is NaN
  double obv_high() const { return obv_max_.max(); }  // max of the last obv_window values

 private:
  std::size_t roc_period_;
  std::size_t count_ = 0;

  // Closes: the SMA-200 needs the value leaving its window, ROC the one
  // roc_period back. 256 slots cover both.
  Ring<256> close_;
  detail::PandasRollingMean sma20_{20}, sma50_{50}, sma200_{200};
  Ring<kMaxMaLag + 1> ma50_hist_, ma200_hist_;   // for arch1 slope / arch2 cross
  double roc_ = std::numeric_limits<double>::quiet_NaN();

  // ADX: the previous bar for this bar's terms, and the last 14 terms so each
  // can be removed from its rolling mean when it leaves the window.
  double prev_high_ = std::numeric_limits<double>::quiet_NaN();
  double prev_low_ = std::numeric_limits<double>::quiet_NaN();
  Ring<kAdxPeriod> tr_, plus_dm_, minus_dm_, dx_;
  detail::PandasRollingMean atr_{kAdxPeriod}, plus_mean_{kAdxPeriod}, minus_mean_{kAdxPeriod},
      adx_{kAdxPeriod};
  double plus_di_ = std::numeric_limits<double>::quiet_NaN();
  double minus_di_ = std::numeric_limits<double>::quiet_NaN();

  // OBV: running cumsum (NaN flow skipped) and its rolling max
  double obv_total_ = 0.0;
  double obv_ = std::numeric_limits<double>::quiet_NaN();
  RollingMax<kMaxObvWindow> obv_max_;
};

}  // namespace mscan::ind
