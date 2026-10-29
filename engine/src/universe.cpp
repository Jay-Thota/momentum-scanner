#include "mscan/universe.h"

#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

#include <bit>
#include <cerrno>
#include <cmath>
#include <cstring>
#include <limits>
#include <stdexcept>
#include <utility>

namespace mscan {
namespace {

// The file stores raw little-endian bytes and the loader maps them in place,
// so a big-endian host would read garbage. Fail at compile time instead.
static_assert(std::endian::native == std::endian::little,
              "MSCANV01 files are little-endian");
// Must match BIN_HEADER in scripts/export_history.py ("<8sIII4xQQ").
static_assert(sizeof(FileHeader) == 40, "FileHeader layout changed");

constexpr char kMagic[8] = {'M', 'S', 'C', 'A', 'N', 'V', '0', '1'};
constexpr uint32_t kVersion = 1;
constexpr uint64_t kNumFields = 5;

[[noreturn]] void fail(const std::string& path, const std::string& what) {
  throw std::runtime_error(path + ": " + what);
}

// Owns a file descriptor for the duration of open_mmap. The mapping stays
// valid after close(), so the fd never needs to outlive the function.
struct FileDescriptor {
  int fd;
  ~FileDescriptor() {
    if (fd >= 0) ::close(fd);
  }
};

void unmap(void* base, std::size_t len) {
  if (base != nullptr) ::munmap(base, len);
}

// first_valid[i] = first day with a non-NaN close, or num_days if none.
std::vector<std::size_t> compute_first_valid(const double* close,
                                             std::size_t num_tickers,
                                             std::size_t num_days) {
  std::vector<std::size_t> first(num_tickers, num_days);
  for (std::size_t i = 0; i < num_tickers; ++i) {
    const double* row = close + i * num_days;
    for (std::size_t day = 0; day < num_days; ++day) {
      if (!std::isnan(row[day])) {
        first[i] = day;
        break;
      }
    }
  }
  return first;
}

}  // namespace

Universe Universe::open_mmap(const std::string& path) {
  FileDescriptor file{::open(path.c_str(), O_RDONLY | O_CLOEXEC)};
  if (file.fd < 0) fail(path, std::string("open failed: ") + std::strerror(errno));

  struct stat st {};
  if (::fstat(file.fd, &st) != 0) fail(path, std::string("fstat failed: ") + std::strerror(errno));
  const auto size = static_cast<std::size_t>(st.st_size);
  if (size < sizeof(FileHeader)) fail(path, "file is smaller than the 40-byte header");

  void* base = ::mmap(nullptr, size, PROT_READ, MAP_PRIVATE, file.fd, 0);
  if (base == MAP_FAILED) fail(path, std::string("mmap failed: ") + std::strerror(errno));

  // Hand the mapping to a Universe immediately: if any check below throws,
  // u's destructor runs during unwinding and unmaps it. No manual cleanup paths.
  Universe u;
  u.map_base_ = base;
  u.map_len_ = size;
  const auto* bytes = static_cast<const char*>(base);

  // memcpy rather than casting bytes to FileHeader*: copying 40 bytes is free
  // and sidesteps alignment and strict-aliasing questions entirely.
  FileHeader h;
  std::memcpy(&h, bytes, sizeof h);
  if (std::memcmp(h.magic, kMagic, sizeof kMagic) != 0) fail(path, "bad magic (not an MSCANV01 file)");
  if (h.version != kVersion) fail(path, "unsupported version " + std::to_string(h.version));

  const uint64_t n = h.num_tickers;
  const uint64_t d = h.num_days;
  if (h.tickers_offset != sizeof(FileHeader) + 4 * d) fail(path, "tickers_offset does not follow the dates");
  if (h.fields_offset < h.tickers_offset || h.fields_offset % 8 != 0) {
    fail(path, "fields_offset is out of order or not 8-byte aligned");
  }
  // n and d are each < 2^32, so n * d fits in 64 bits; the byte count might
  // not, so check before multiplying by 40.
  const uint64_t cells = n * d;
  if (cells > (std::numeric_limits<uint64_t>::max() - h.fields_offset) / (kNumFields * 8)) {
    fail(path, "header dimensions overflow");
  }
  const uint64_t expected = h.fields_offset + kNumFields * 8 * cells;
  if (expected != size) {
    fail(path, "size " + std::to_string(size) + " bytes, header implies " +
                   std::to_string(expected) + " (truncated or corrupt)");
  }

  u.dates_.resize(d);
  std::memcpy(u.dates_.data(), bytes + sizeof(FileHeader), 4 * d);

  // Ticker names: n NUL-terminated strings packed between the two offsets.
  const char* p = bytes + h.tickers_offset;
  const char* end = bytes + h.fields_offset;
  u.tickers_.reserve(n);
  for (uint64_t i = 0; i < n; ++i) {
    const auto* nul = static_cast<const char*>(std::memchr(p, '\0', static_cast<std::size_t>(end - p)));
    if (nul == nullptr) fail(path, "ticker blob ends after " + std::to_string(i) + " names");
    u.tickers_.emplace_back(p, nul);
    p = nul + 1;
  }

  // The five field arrays are read in place, no copy. mmap returns a
  // page-aligned base and fields_offset is a multiple of 8, so every double
  // is correctly aligned.
  const auto* fields = reinterpret_cast<const double*>(bytes + h.fields_offset);
  u.open_ = fields;
  u.high_ = fields + cells;
  u.low_ = fields + 2 * cells;
  u.close_ = fields + 3 * cells;
  u.volume_ = fields + 4 * cells;

  u.first_valid_ = compute_first_valid(u.close_, n, d);
  return u;  // moved (or elided) out; the mapping travels with it
}

Universe Universe::from_buffers(std::vector<std::string> tickers,
                                std::vector<int32_t> dates,
                                const double* open, const double* high,
                                const double* low, const double* close,
                                const double* volume) {
  const std::size_t n = tickers.size();
  const std::size_t d = dates.size();
  if (n * d > 0 && (open == nullptr || high == nullptr || low == nullptr ||
                    close == nullptr || volume == nullptr)) {
    throw std::invalid_argument("from_buffers: null field buffer");
  }

  Universe u;  // map_base_ stays null, so the destructor frees nothing
  u.tickers_ = std::move(tickers);
  u.dates_ = std::move(dates);
  u.open_ = open;
  u.high_ = high;
  u.low_ = low;
  u.close_ = close;
  u.volume_ = volume;
  u.first_valid_ = compute_first_valid(close, n, d);
  return u;
}

// Moving steals the mapping and leaves the source empty, so exactly one
// object ever calls munmap on it.
Universe::Universe(Universe&& o) noexcept
    : tickers_(std::move(o.tickers_)),
      dates_(std::move(o.dates_)),
      first_valid_(std::move(o.first_valid_)),
      open_(std::exchange(o.open_, nullptr)),
      high_(std::exchange(o.high_, nullptr)),
      low_(std::exchange(o.low_, nullptr)),
      close_(std::exchange(o.close_, nullptr)),
      volume_(std::exchange(o.volume_, nullptr)),
      map_base_(std::exchange(o.map_base_, nullptr)),
      map_len_(std::exchange(o.map_len_, 0)) {}

Universe& Universe::operator=(Universe&& o) noexcept {
  if (this != &o) {
    unmap(map_base_, map_len_);  // release our own mapping first
    tickers_ = std::move(o.tickers_);
    dates_ = std::move(o.dates_);
    first_valid_ = std::move(o.first_valid_);
    open_ = std::exchange(o.open_, nullptr);
    high_ = std::exchange(o.high_, nullptr);
    low_ = std::exchange(o.low_, nullptr);
    close_ = std::exchange(o.close_, nullptr);
    volume_ = std::exchange(o.volume_, nullptr);
    map_base_ = std::exchange(o.map_base_, nullptr);
    map_len_ = std::exchange(o.map_len_, 0);
  }
  return *this;
}

Universe::~Universe() { unmap(map_base_, map_len_); }

BarSeries Universe::series(std::size_t ticker_idx, std::size_t end_day) const {
  if (ticker_idx >= num_tickers()) throw std::out_of_range("series: ticker index out of range");
  if (end_day >= num_days()) throw std::out_of_range("series: end_day out of range");

  // Empty when the ticker has no bar yet on end_day (or none at all, where
  // first == num_days). Interior NaN holes after the first bar are kept.
  const std::size_t first = first_valid_[ticker_idx];
  if (first > end_day) return {};

  const std::size_t row = ticker_idx * num_days() + first;
  const std::size_t len = end_day - first + 1;
  return {{open_ + row, len}, {high_ + row, len}, {low_ + row, len},
          {close_ + row, len}, {volume_ + row, len}};
}

std::size_t Universe::first_valid_day(std::size_t ticker_idx) const {
  return first_valid_.at(ticker_idx);
}

}  // namespace mscan
