cmake_minimum_required(VERSION 3.20)
project(mscan_engine LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)   # core lib gets linked into the .so

option(MSCAN_SANITIZE "Build with ASan + UBSan" OFF)
option(MSCAN_TSAN "Build with ThreadSanitizer (exclusive with ASan)" OFF)
# OFF until phase 3 writes the bindings (bindings.cpp still has undefined TODOs)
option(MSCAN_PYTHON "Build pybind11 module" OFF)

if(MSCAN_SANITIZE)
  add_compile_options(-fsanitize=address,undefined -fno-omit-frame-pointer)
  add_link_options(-fsanitize=address,undefined)
endif()
if(MSCAN_TSAN)
  add_compile_options(-fsanitize=thread)
  add_link_options(-fsanitize=thread)
endif()

include(FetchContent)
FetchContent_Declare(googletest
  URL https://github.com/google/googletest/archive/refs/tags/v1.15.2.tar.gz)
FetchContent_Declare(benchmark
  URL https://github.com/google/benchmark/archive/refs/tags/v1.9.0.tar.gz)
set(BENCHMARK_ENABLE_TESTING OFF CACHE BOOL "" FORCE)
FetchContent_MakeAvailable(googletest benchmark)

# ---- core library -----------------------------------------------------------
add_library(mscan_core
  src/universe.cpp
  src/indicators.cpp
  src/layers.cpp
  src/thread_pool.cpp
  src/backtest.cpp)
target_include_directories(mscan_core PUBLIC include)
target_compile_options(mscan_core PRIVATE -Wall -Wextra -Wpedantic -Werror
  # Parity with numpy/pandas: never fuse a*b+c into one FMA (one rounding
  # instead of two), which would drift results by a few ULPs.
  -ffp-contract=off)

# ---- tests ------------------------------------------------------------------
enable_testing()
add_executable(mscan_tests
  tests/universe_test.cpp       # mmap loader: bad magic, truncation, NaN left-padding
  tests/indicators_test.cpp     # hand-computed small cases + NaN warm-up
  tests/parity_test.cpp         # vs. golden CSVs dumped from the Python layers
  tests/thread_pool_test.cpp
  tests/backtest_test.cpp)      # look-ahead guard, synthetic known-answer data
target_link_libraries(mscan_tests PRIVATE mscan_core GTest::gtest_main)
# Tests find engine/data/ and engine/tests/golden/ relative to this
target_compile_definitions(mscan_tests PRIVATE MSCAN_ENGINE_DIR="${CMAKE_CURRENT_SOURCE_DIR}")
include(GoogleTest)
gtest_discover_tests(mscan_tests)

# ---- benchmarks -------------------------------------------------------------
add_executable(mscan_bench bench/score_bench.cpp)
target_link_libraries(mscan_bench PRIVATE mscan_core benchmark::benchmark_main)

# ---- python module ----------------------------------------------------------
if(MSCAN_PYTHON)
  find_package(pybind11 CONFIG REQUIRED)   # pip install pybind11
  pybind11_add_module(mscan_engine python/bindings.cpp)
  target_link_libraries(mscan_engine PRIVATE mscan_core)
endif()
