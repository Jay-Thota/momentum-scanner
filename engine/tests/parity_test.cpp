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
#include <optional>
#include <span>
#include <stdexcept>
#include <sstream>
#include <string>
#include <vector>

#include "mscan/indicators.h"
#include "mscan/layers.h"

namespace {

namespace fs = std::filesystem;
namespace ind = mscan::ind;

struct GoldenSeries {
  std::string name;
  std::vector<std::string> dates;   // YYYY-MM-DD
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
    std::getline(ss, cell, ',');
    g.dates.push_back(cell);
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

TEST(Parity, AdxMatchesPython) {
  for (const auto& g : goldens()) {
    const std::size_t n = g.size();
    std::vector<double> adx(n), plus_di(n), minus_di(n);
    mscan::BarSeries bars{g.col("Open"), g.col("High"), g.col("Low"), g.col("Close"), g.col("Volume")};
    ind::adx(bars, 14, {adx, plus_di, minus_di});   // period hard-coded in layer2._add_indicators
    expect_bit_identical(adx, g.col("adx"), g.name, "adx");
    expect_bit_identical(plus_di, g.col("di_p"), g.name, "di_p");
    expect_bit_identical(minus_di, g.col("di_n"), g.name, "di_n");
  }
}

TEST(Parity, ObvMatchesPython) {
  for (const auto& g : goldens()) {
    std::vector<double> out(g.size());
    ind::obv(g.col("Close"), g.col("Volume"), out);
    expect_bit_identical(out, g.col("OBV"), g.name, "OBV");
  }
}

// ---------------------------------------------------------------------------
// Layer 1 / Layer 2 vs results.csv
// ---------------------------------------------------------------------------

namespace {

using Row = std::map<std::string, std::string>;
const fs::path kGoldenDir = fs::path(MSCAN_ENGINE_DIR) / "tests" / "golden";

std::vector<Row> load_rows(const fs::path& path) {
  std::ifstream in(path);
  std::string line, cell;
  std::getline(in, line);
  std::vector<std::string> header;
  for (std::stringstream ss(line); std::getline(ss, cell, ',');) header.push_back(cell);
  std::vector<Row> rows;
  while (std::getline(in, line)) {
    Row row;
    std::stringstream ss(line);
    for (const auto& h : header) {
      if (!std::getline(ss, cell, ',')) cell.clear();   // trailing empty fields
      row[h] = cell;
    }
    rows.push_back(std::move(row));
  }
  return rows;
}

const GoldenSeries& series_named(const std::string& name) {
  for (const auto& g : goldens()) {
    if (g.name == name) return g;
  }
  throw std::runtime_error("no golden series " + name);
}

mscan::BarSeries bars_of(const GoldenSeries& g, std::size_t n) {
  auto take = [&](const char* c) { return std::span<const double>(g.col(c).data(), n); };
  return {take("Open"), take("High"), take("Low"), take("Close"), take("Volume")};
}

// The sector ETF frame dump_golden passed to layer2: the ETF cut at as_of.
std::optional<mscan::SectorContext> sector_for(const Row& row, std::map<std::string, GoldenSeries>& etfs) {
  const std::string& etf = row.at("sector_etf");
  if (etf.empty()) return std::nullopt;
  if (!etfs.count(etf)) etfs[etf] = load_series(kGoldenDir / "etf" / (etf + ".csv"));
  const GoldenSeries& g = etfs[etf];
  const auto n = static_cast<std::size_t>(
      std::upper_bound(g.dates.begin(), g.dates.end(), row.at("as_of")) - g.dates.begin());
  return mscan::SectorContext{bars_of(g, n), std::nullopt};
}

// "" is Python None (NaN here); "1"/"0" are bools.
double num(const std::string& s) { return s.empty() ? std::nan("") : std::strtod(s.c_str(), nullptr); }
std::string flag(bool b) { return b ? "1" : "0"; }

void expect_num(double got, const Row& row, const char* col) {
  const double want = num(row.at(col));
  if (std::isnan(want)) {
    EXPECT_TRUE(std::isnan(got)) << row.at("case") << " " << col << ": want None, got " << got;
  } else {
    EXPECT_EQ(got, want) << row.at("case") << " " << col;
  }
}

}  // namespace

TEST(Parity, Layer1MatchesPython) {
  const mscan::ScoringConfig cfg;   // defaults mirror config.py (see config.csv)
  for (const auto& row : load_rows(kGoldenDir / "results.csv")) {
    const auto& g = series_named(row.at("case"));
    const auto r = mscan::score_layer1(bars_of(g, g.size()), /*block_trades=*/0, cfg);
    const std::string& c = row.at("case");
    expect_num(r.score, row, "l1_score");
    if (row.at("l1_A").empty()) {   // Python returned signals == {}
      EXPECT_FALSE(r.valid) << c;
      continue;
    }
    ASSERT_TRUE(r.valid) << c;
    EXPECT_EQ(flag(r.A), row.at("l1_A")) << c;
    EXPECT_EQ(flag(r.B), row.at("l1_B")) << c;
    EXPECT_EQ(flag(r.C), row.at("l1_C")) << c;
    EXPECT_EQ(flag(r.D), row.at("l1_D")) << c;
    EXPECT_EQ(flag(r.E), row.at("l1_E")) << c;
    expect_num(r.vol_ratio, row, "l1_vol_ratio");
    expect_num(r.vol_ratio, row, "l1_rvol");
    expect_num(r.up_down_ratio, row, "l1_up_down_ratio");
    EXPECT_EQ(std::to_string(r.block_trades), row.at("l1_block_trades")) << c;
    EXPECT_EQ(std::to_string(r.signals_fired), row.at("l1_signals_fired")) << c;
  }
}

TEST(Parity, Layer2MatchesPython) {
  const mscan::ScoringConfig cfg;
  std::map<std::string, GoldenSeries> etfs;
  std::map<std::string, int> archetypes_seen;
  for (const auto& row : load_rows(kGoldenDir / "results.csv")) {
    const auto& g = series_named(row.at("case"));
    const auto r = mscan::score_layer2(bars_of(g, g.size()), sector_for(row, etfs), cfg);
    const std::string& c = row.at("case");

    const std::string arch = r.archetype ? std::to_string(*r.archetype) : "";
    EXPECT_EQ(arch, row.at("l2_archetype")) << c;
    ++archetypes_seen[arch];
    expect_num(r.archetype_score, row, "l2_archetype_score");
    expect_num(r.momentum_score, row, "l2_momentum_score");
    expect_num(r.total, row, "l2_total");
    if (row.at("l2_A").empty()) {   // Python's _empty: signals == {}
      EXPECT_FALSE(r.valid) << c;
      continue;
    }
    ASSERT_TRUE(r.valid) << c;
    EXPECT_EQ(flag(r.mom_A), row.at("l2_A")) << c;
    EXPECT_EQ(flag(r.mom_B), row.at("l2_B")) << c;
    expect_num(r.ma_pts, row, "l2_C_pts");
    expect_num(r.roc10, row, "l2_roc10");
    expect_num(r.adx, row, "l2_adx");
    expect_num(r.di_p, row, "l2_di_p");
    expect_num(r.di_n, row, "l2_di_n");
    EXPECT_EQ(std::to_string(r.dist_days), row.at("l2_dist_days")) << c;
    expect_num(r.close_pct, row, "l2_close_pct");

    const std::string bo_date = r.breakout_day >= 0 ? g.dates.at(static_cast<std::size_t>(r.breakout_day)) : "";
    EXPECT_EQ(bo_date, row.at("l2_breakout_date")) << c;
    if (r.breakout_day >= 0) {
      expect_num(r.breakout_pct, row, "l2_breakout_pct_chg");
      expect_num(r.breakout_vol_ratio, row, "l2_breakout_vol_ratio");
    }
  }
  // The golden set must keep exercising every archetype
  for (const char* a : {"1", "2", "3", "4", ""}) EXPECT_GT(archetypes_seen[a], 0) << "archetype '" << a << "'";
}
