// Parity against the golden CSVs written by scripts/dump_golden.py.
//
// Every series file holds the input OHLCV plus each indicator as computed by
// the Python reference. Kernels must reproduce them bit for bit: NaN where
// Python has NaN, otherwise the identical double. Values are parsed with
// strtod, which is correctly rounded, so the repr() strings in the CSV come
// back as exactly the doubles Python wrote.

#include <gtest/gtest.h>

#include <algorithm>
#include <bit>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <map>
#include <sstream>
#include <string>
#include <vector>

#include "mscan/indicators.h"

namespace {

namespace fs = std::filesystem;
namespace ind = mscan::ind;

struct GoldenSeries {
  std::string name;
  std::map<std::string, std::vector<double>> cols;
  const std::vector<double>& col(const std::string& c) const { return cols.at(c); }
  std::size_t size() const { return cols.at("Close").size(); }
};

GoldenSeries load_series(const fs::path& path) {
  GoldenSeries g;
  g.name = path.stem().string();
  std::ifstream in(path);
  std::string line, cell;
  std::getline(in, line);
  std::vector<std::string> header;
  for (std::stringstream ss(line); std::getline(ss, cell, ',');) header.push_back(cell);
  while (std::getline(in, line)) {
    std::stringstream ss(line);
    std::getline(ss, cell, ',');   // date
    for (std::size_t c = 1; c < header.size() && std::getline(ss, cell, ','); ++c) {
      g.cols[header[c]].push_back(std::strtod(cell.c_str(), nullptr));   // "nan" -> NaN
    }
  }
  return g;
}

std::vector<GoldenSeries> load_all() {
  const fs::path dir = fs::path(MSCAN_ENGINE_DIR) / "tests" / "golden" / "series";
  std::vector<fs::path> files;
  for (const auto& e : fs::directory_iterator(dir)) files.push_back(e.path());
  std::sort(files.begin(), files.end());
  std::vector<GoldenSeries> out;
  for (const auto& f : files) out.push_back(load_series(f));
  return out;
}

const std::vector<GoldenSeries>& goldens() {
  static const std::vector<GoldenSeries> all = load_all();
  return all;
}

int64_t ulp_distance(double a, double b) {
  const auto ia = std::bit_cast<int64_t>(a), ib = std::bit_cast<int64_t>(b);
  return ia > ib ? ia - ib : ib - ia;
}

// Exact match or a failure naming the first differing bar and the ULP gap.
void expect_bit_identical(const std::vector<double>& got, const std::vector<double>& want,
                          const std::string& series, const std::string& column) {
  ASSERT_EQ(got.size(), want.size()) << series << " " << column;
  std::size_t bad = 0, first = 0;
  for (std::size_t i = 0; i < want.size(); ++i) {
    const bool same = std::isnan(want[i]) ? std::isnan(got[i]) : got[i] == want[i];
    if (!same && bad++ == 0) first = i;
  }
  EXPECT_EQ(bad, 0u) << series << " " << column << ": " << bad << " of " << want.size()
                     << " bars differ; first at bar " << first << " got " << got[first]
                     << " want " << want[first] << " ("
                     << (std::isnan(got[first]) || std::isnan(want[first])
                             ? std::string("NaN mismatch")
                             : std::to_string(ulp_distance(got[first], want[first])) + " ULPs")
                     << ")";
}

}  // namespace

TEST(Parity, GoldenSetLoaded) {
  ASSERT_GE(goldens().size(), 68u) << "run: python -m scripts.dump_golden --end 2026-10-28";
  for (const auto& g : goldens()) EXPECT_GT(g.size(), 0u) << g.name;
}

TEST(Parity, SmaMatchesPandasRollingMean) {
  for (const auto& g : goldens()) {
    for (auto [period, column] : {std::pair{20, "ma20"}, {50, "ma50"}, {200, "ma200"}}) {
      std::vector<double> out(g.size());
      ind::sma(g.col("Close"), period, out);
      expect_bit_identical(out, g.col(column), g.name, column);
    }
  }
}

TEST(Parity, RocMatchesPython) {
  for (const auto& g : goldens()) {
    std::vector<double> out(g.size());
    ind::roc(g.col("Close"), 10, out);   // ROC_PERIOD default, see config.csv
    expect_bit_identical(out, g.col("roc10"), g.name, "roc10");
  }
}

TEST(Parity, ObvMatchesPython) {
  for (const auto& g : goldens()) {
    std::vector<double> out(g.size());
    ind::obv(g.col("Close"), g.col("Volume"), out);
    expect_bit_identical(out, g.col("OBV"), g.name, "OBV");
  }
}
