#pragma once
// Heap allocations seen so far by the test binary. indicators_test.cpp
// replaces the global operator new to count them; NoHeapAllocation tests
// compare this before and after a call.

namespace mscan_test {
long allocations();
}  // namespace mscan_test
