#pragma once
// Columnar storage for the whole universe of daily bars.
//
// Layout: every field (open/high/low/close/volume) is one contiguous array of
// num_tickers * num_days doubles, indexed [ticker * num_days + day]. All tickers
// share one trading calendar; days before a ticker's first bar (IPO, late listing)
// hold NaN. That costs some padding but makes cross-sectional steps (RS rank,
// "who qualifies on day d") a simple column walk and keeps backtest alignment trivial.
//
// Two ways in:
//   - Universe::open_mmap(path): backtests over years of history, file stays on disk
//   - Universe::from_buffers(...): daily scan, zero-copy over NumPy arrays from Python

#include <cstddef>
#include <cstdint>
#include <span>
#include <string>
#include <vector>

namespace mscan {

// Non-owning view of one ticker's bars [0, end] (inclusive). Everything that
// scores "as of day d" takes one of these, so code physically cannot read d+1.
struct BarSeries {
  std::span<const double> open, high, low, close, volume;
  std::size_t size() const { return close.size(); }
  bool empty() const { return close.empty(); }
};

// On-disk header for the binary file written by scripts/export_history.py.
// Fixed-size, little-endian, followed by: dates[num_days] (int32 YYYYMMDD),
// tickers blob, then the five field arrays back to back.
struct FileHeader {
  char magic[8];          // "MSCANV01"
  uint32_t version;
  uint32_t num_tickers;
  uint32_t num_days;
  uint64_t tickers_offset;
  uint64_t fields_offset;
};

class Universe {
 public:
  // Maps the file read-only. Throws std::runtime_error on bad magic/size.
  static Universe open_mmap(const std::string& path);

  // Wraps caller-owned buffers (no copy). Caller keeps them alive.
  static Universe from_buffers(std::vector<std::string> tickers,
                               std::vector<int32_t> dates,
                               const double* open, const double* high,
                               const double* low, const double* close,
                               const double* volume);

  Universe(Universe&&) noexcept;
  Universe& operator=(Universe&&) noexcept;
  Universe(const Universe&) = delete;             // owns an mmap; no copies
  Universe& operator=(const Universe&) = delete;
  ~Universe();                                     // munmap if mapped

  std::size_t num_tickers() const { return tickers_.size(); }
  std::size_t num_days() const { return dates_.size(); }
  const std::string& ticker(std::size_t i) const { return tickers_[i]; }
  int32_t date(std::size_t day) const { return dates_[day]; }

  // Bars for ticker i from its first valid bar through `end_day` inclusive.
  BarSeries series(std::size_t ticker_idx, std::size_t end_day) const;

  // Index of the first non-NaN bar for ticker i (precomputed at load).
  std::size_t first_valid_day(std::size_t ticker_idx) const;

 private:
  Universe() = default;

  std::vector<std::string> tickers_;
  std::vector<int32_t> dates_;
  std::vector<std::size_t> first_valid_;
  const double* open_ = nullptr;
  const double* high_ = nullptr;
  const double* low_ = nullptr;
  const double* close_ = nullptr;
  const double* volume_ = nullptr;

  void* map_base_ = nullptr;   // non-null only when mmapped
  std::size_t map_len_ = 0;
};

}  // namespace mscan
