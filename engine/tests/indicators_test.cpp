#include <gtest/gtest.h>

#include "mscan/indicators.h"

// Smoke test: the public headers compile and link against mscan_core.
TEST(Smoke, EmptyBarSeries) {
  mscan::BarSeries s;
  EXPECT_TRUE(s.empty());
  EXPECT_EQ(s.size(), 0u);
}
