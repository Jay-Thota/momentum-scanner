#pragma once
// Golden series written by scripts/dump_golden.py (engine/tests/golden/series):
// input OHLCV plus each indicator as computed by the Python reference.
// Values are parsed with strtod, which is correctly rounded, so the repr()
// strings in the CSV come back as exactly the doubles Python wrote.

#include <algorithm>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <map>
#include <sstream>
#include <string>
#include <vector>

#include "mscan/universe.h"

namespace mscan_test {

struct GoldenSeries {
  std::string name;
  std::vector<std::string> dates;   // YYYY-MM-DD
  std::map<std::string, std::vector<double>> cols;
  const std::vector<double>& col(const std::string& c) const { return cols.at(c); }
  std::size_t size() const { return cols.at("Close").size(); }
};

inline GoldenSeries load_series(const std::filesystem::path& path) {
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

inline std::vector<GoldenSeries> load_all() {
  const std::filesystem::path dir = std::filesystem::path(MSCAN_ENGINE_DIR) / "tests" / "golden" / "series";
  std::vector<std::filesystem::path> files;
  for (const auto& e : std::filesystem::directory_iterator(dir)) files.push_back(e.path());
  std::sort(files.begin(), files.end());
  std::vector<GoldenSeries> out;
  for (const auto& f : files) out.push_back(load_series(f));
  return out;
}

inline const std::vector<GoldenSeries>& goldens() {
  static const std::vector<GoldenSeries> all = load_all();
  return all;
}

// The input bars as a BarSeries (spans into g's columns)
inline mscan::BarSeries bars_of(const GoldenSeries& g) {
  return {g.col("Open"), g.col("High"), g.col("Low"), g.col("Close"), g.col("Volume")};
}

}  // namespace mscan_test
