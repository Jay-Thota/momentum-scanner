// py_round / np_round against Python's own results (scripts/dump_rounding_golden.py).

#include <gtest/gtest.h>

#include <cmath>
#include <cstdlib>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>

#include "mscan/pyround.h"

namespace {

// Same value, and for zero the same sign (round(-0.0001, 3) is -0.0 in Python).
bool same(double a, double b) { return a == b && std::signbit(a) == std::signbit(b); }

}  // namespace

TEST(Rounding, MatchesPythonAndNumpy) {
  std::ifstream in(std::string(MSCAN_ENGINE_DIR) + "/tests/golden/rounding.csv");
  ASSERT_TRUE(in) << "run: python -m scripts.dump_rounding_golden";
  std::string line;
  std::getline(in, line);   // header: x,py2,py3,np2,np3
  std::size_t rows = 0, bad = 0;
  while (std::getline(in, line)) {
    std::vector<double> v;
    std::stringstream ss(line);
    for (std::string cell; std::getline(ss, cell, ',');) v.push_back(std::strtod(cell.c_str(), nullptr));
    ASSERT_EQ(v.size(), 5u) << line;
    const double x = v[0];
    const bool ok = same(mscan::py_round(x, 2), v[1]) && same(mscan::py_round(x, 3), v[2]) &&
                    same(mscan::np_round(x, 2), v[3]) && same(mscan::np_round(x, 3), v[4]);
    if (!ok && bad++ < 10) {
      ADD_FAILURE() << "x=" << line.substr(0, line.find(',')) << " py2=" << mscan::py_round(x, 2)
                    << " py3=" << mscan::py_round(x, 3) << " np2=" << mscan::np_round(x, 2)
                    << " np3=" << mscan::np_round(x, 3) << " want " << line;
    }
    ++rows;
  }
  EXPECT_GT(rows, 10000u);
  EXPECT_EQ(bad, 0u) << bad << " of " << rows << " rows differ";
}

TEST(Rounding, KnownDisagreements) {
  // The cases that make two helpers necessary
  EXPECT_EQ(mscan::py_round(2.675, 2), 2.67);   // exact value is 2.67499999...
  EXPECT_EQ(mscan::np_round(2.675, 2), 2.68);   // 2.675 * 100 rounds up to 267.5
  EXPECT_TRUE(std::isnan(mscan::py_round(NAN, 3)));
  EXPECT_TRUE(std::isnan(mscan::np_round(NAN, 3)));
}
