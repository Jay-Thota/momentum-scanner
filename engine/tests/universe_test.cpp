#include <gtest/gtest.h>
#include <unistd.h>

#include <cmath>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>

#include "mscan/universe.h"

namespace {

using mscan::FileHeader;
using mscan::Universe;

constexpr double kNaN = std::numeric_limits<double>::quiet_NaN();

// 3 tickers x 5 days. Value = ticker * 100 + day, field f adds f * 1000.
//   AAA: full history
//   BBB: first two days NaN (late listing)
//   CCC: all NaN (no bars at all)
struct Fixture {
  std::vector<std::string> tickers = {"AAA", "BBB", "CCC"};
  std::vector<int32_t> dates = {20260101, 20260102, 20260105, 20260106, 20260107};
  std::vector<double> fields;   // 5 fields back to back, [f][ticker][day]

  Fixture() {
    const std::size_t n = tickers.size(), d = dates.size();
    fields.resize(5 * n * d);
    for (std::size_t f = 0; f < 5; ++f)
      for (std::size_t i = 0; i < n; ++i)
        for (std::size_t day = 0; day < d; ++day) {
          const bool missing = (i == 1 && day < 2) || i == 2;
          fields[(f * n + i) * d + day] = missing ? kNaN : f * 1000.0 + i * 100.0 + day;
        }
  }
  const double* field(std::size_t f) const { return fields.data() + f * tickers.size() * dates.size(); }

  // Serialize in the MSCANV01 layout written by scripts/export_history.py.
  std::vector<char> bytes() const {
    std::string blob;
    for (const auto& t : tickers) blob += t + '\0';
    FileHeader h{};
    std::memcpy(h.magic, "MSCANV01", 8);
    h.version = 1;
    h.num_tickers = static_cast<uint32_t>(tickers.size());
    h.num_days = static_cast<uint32_t>(dates.size());
    h.tickers_offset = sizeof(FileHeader) + 4 * dates.size();
    h.fields_offset = (h.tickers_offset + blob.size() + 7) / 8 * 8;

    std::vector<char> out(h.fields_offset + fields.size() * sizeof(double), '\0');
    std::memcpy(out.data(), &h, sizeof h);
    std::memcpy(out.data() + sizeof h, dates.data(), 4 * dates.size());
    std::memcpy(out.data() + h.tickers_offset, blob.data(), blob.size());
    std::memcpy(out.data() + h.fields_offset, fields.data(), fields.size() * sizeof(double));
    return out;
  }
};

// Writes bytes to a per-test temp file and removes it afterwards.
class TempFile {
 public:
  explicit TempFile(const std::vector<char>& bytes) {
    const auto* info = ::testing::UnitTest::GetInstance()->current_test_info();
    path_ = std::filesystem::temp_directory_path() /
            (std::string("mscan_") + info->name() + "_" + std::to_string(::getpid()) + ".bin");
    std::ofstream(path_, std::ios::binary).write(bytes.data(), static_cast<std::streamsize>(bytes.size()));
  }
  ~TempFile() { std::filesystem::remove(path_); }
  std::string path() const { return path_.string(); }

 private:
  std::filesystem::path path_;
};

void expect_fixture_layout(const Universe& u) {
  ASSERT_EQ(u.num_tickers(), 3u);
  ASSERT_EQ(u.num_days(), 5u);
  EXPECT_EQ(u.ticker(1), "BBB");
  EXPECT_EQ(u.date(4), 20260107);

  // NaN left-padding: first_valid_day skips the padded days
  EXPECT_EQ(u.first_valid_day(0), 0u);
  EXPECT_EQ(u.first_valid_day(1), 2u);
  EXPECT_EQ(u.first_valid_day(2), 5u);   // never valid == num_days

  auto a = u.series(0, 4);
  ASSERT_EQ(a.size(), 5u);
  EXPECT_EQ(a.close[0], 3000.0);
  EXPECT_EQ(a.volume[4], 4004.0);

  auto b = u.series(1, 4);               // starts at its first real bar
  ASSERT_EQ(b.size(), 3u);
  EXPECT_EQ(b.open[0], 102.0);
  EXPECT_EQ(b.high[2], 1104.0);
  for (double v : b.close) EXPECT_FALSE(std::isnan(v));

  EXPECT_TRUE(u.series(1, 1).empty());   // before its first bar
  EXPECT_TRUE(u.series(2, 4).empty());   // no bars at all

  // As-of slicing: day 2's view physically ends at day 2 (no look-ahead)
  auto a2 = u.series(0, 2);
  ASSERT_EQ(a2.size(), 3u);
  EXPECT_EQ(a2.close.back(), 3002.0);
}

}  // namespace

TEST(UniverseMmap, OpensValidFile) {
  Fixture fx;
  TempFile file(fx.bytes());
  Universe u = Universe::open_mmap(file.path());
  expect_fixture_layout(u);
}

TEST(UniverseMmap, BadMagicThrows) {
  auto bytes = Fixture().bytes();
  bytes[0] = 'X';
  TempFile file(bytes);
  EXPECT_THROW(Universe::open_mmap(file.path()), std::runtime_error);
}

TEST(UniverseMmap, WrongVersionThrows) {
  auto bytes = Fixture().bytes();
  bytes[8] = 2;   // version is the u32 right after the magic
  TempFile file(bytes);
  EXPECT_THROW(Universe::open_mmap(file.path()), std::runtime_error);
}

TEST(UniverseMmap, TruncatedFileThrows) {
  auto bytes = Fixture().bytes();
  bytes.resize(bytes.size() - 8);   // drop the last volume value
  TempFile file(bytes);
  EXPECT_THROW(Universe::open_mmap(file.path()), std::runtime_error);
}

TEST(UniverseMmap, FileSmallerThanHeaderThrows) {
  auto bytes = Fixture().bytes();
  bytes.resize(20);
  TempFile file(bytes);
  EXPECT_THROW(Universe::open_mmap(file.path()), std::runtime_error);
}

TEST(UniverseMmap, MissingFileThrows) {
  EXPECT_THROW(Universe::open_mmap("/nonexistent/mscan/history.bin"), std::runtime_error);
}

TEST(UniverseMmap, MoveTransfersTheMapping) {
  Fixture fx;
  TempFile file(fx.bytes());
  Universe a = Universe::open_mmap(file.path());
  Universe b = std::move(a);          // b now owns the mapping
  expect_fixture_layout(b);

  Universe c = Universe::open_mmap(file.path());
  c = std::move(b);                   // c unmaps its own mapping, takes b's
  expect_fixture_layout(c);
}

TEST(UniverseMmap, OutOfRangeThrows) {
  Fixture fx;
  TempFile file(fx.bytes());
  Universe u = Universe::open_mmap(file.path());
  EXPECT_THROW(u.series(3, 0), std::out_of_range);
  EXPECT_THROW(u.series(0, 5), std::out_of_range);
  EXPECT_THROW(u.first_valid_day(3), std::out_of_range);
}

TEST(UniverseBuffers, WrapsWithoutCopying) {
  Fixture fx;
  Universe u = Universe::from_buffers(fx.tickers, fx.dates, fx.field(0), fx.field(1),
                                      fx.field(2), fx.field(3), fx.field(4));
  expect_fixture_layout(u);
  // The span points into the caller's buffer: BBB's day-2 close
  EXPECT_EQ(u.series(1, 4).close.data(), fx.field(3) + 1 * 5 + 2);
}

TEST(UniverseBuffers, NullBufferThrows) {
  Fixture fx;
  EXPECT_THROW(Universe::from_buffers(fx.tickers, fx.dates, nullptr, fx.field(1),
                                      fx.field(2), fx.field(3), fx.field(4)),
               std::invalid_argument);
}

// The real export, when present (engine/data/ is gitignored, so CI skips this).
TEST(UniverseMmap, RealHistoryFile) {
  const std::string path = std::string(MSCAN_ENGINE_DIR) + "/data/history.bin";
  if (!std::filesystem::exists(path)) GTEST_SKIP() << path << " not found";
  Universe u = Universe::open_mmap(path);
  EXPECT_EQ(u.num_tickers(), 3289u);
  EXPECT_EQ(u.num_days(), 1003u);
  EXPECT_EQ(u.date(0), 20221028);
  EXPECT_EQ(u.ticker(0), "SPY");
  EXPECT_EQ(u.first_valid_day(0), 0u);
  EXPECT_DOUBLE_EQ(u.series(0, 1002).close.back(), 772.28);
  for (std::size_t i = 0; i < u.num_tickers(); ++i) {
    if (u.ticker(i) == "KLAR") {
      EXPECT_EQ(u.first_valid_day(i), 717u);   // IPO 2025-09-10, left-padded before that
      EXPECT_EQ(u.date(717), 20250910);
    }
  }
}
