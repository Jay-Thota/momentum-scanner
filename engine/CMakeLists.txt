cmake_minimum_required(VERSION 3.20)
project(mscan_engine LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)   # core lib gets linked into the .so

option(MSCAN_SANITIZE "Build with ASan + UBSan" OFF)
option(MSCAN_TSAN "Build with ThreadSanitizer (exclusive with ASan)" OFF)
option(MSCAN_PYTHON "Build pybind11 module" ON)
# pip install sets this OFF so building the module never downloads gtest/benchmark
option(MSCAN_BUILD_TESTS "Build tests and benchmarks" ON)

if(MSCAN_SANITIZE)
  add_compile_options(-fsanitize=address,undefined -fno-omit-frame-pointer)
  add_link_options(-fsanitize=address,undefined)
endif()
if(MSCAN_TSAN)
  add_compile_options(-fsanitize=thread)
  add_link_options(-fsanitize=thread)
endif()

# ---- core library -----------------------------------------------------------
add_library(mscan_core STATIC
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

if(MSCAN_BUILD_TESTS)
  include(FetchContent)
  FetchContent_Declare(googletest
    URL https://github.com/google/googletest/archive/refs/tags/v1.15.2.tar.gz
    DOWNLOAD_EXTRACT_TIMESTAMP TRUE)
  FetchContent_Declare(benchmark
    URL https://github.com/google/benchmark/archive/refs/tags/v1.9.0.tar.gz
    DOWNLOAD_EXTRACT_TIMESTAMP TRUE)
  set(BENCHMARK_ENABLE_TESTING OFF CACHE BOOL "" FORCE)
  FetchContent_MakeAvailable(googletest benchmark)

  # ---- tests ----------------------------------------------------------------
  enable_testing()
  add_executable(mscan_tests
    tests/universe_test.cpp       # mmap loader: bad magic, truncation, NaN left-padding
    tests/indicators_test.cpp     # hand-computed small cases + NaN warm-up
    tests/rounding_test.cpp       # py_round / np_round vs Python
    tests/parity_test.cpp         # vs. golden CSVs dumped from the Python layers
    tests/thread_pool_test.cpp
    tests/backtest_test.cpp)      # look-ahead guard, synthetic known-answer data
  target_link_libraries(mscan_tests PRIVATE mscan_core GTest::gtest_main)
  # Tests find engine/data/ and engine/tests/golden/ relative to this
  target_compile_definitions(mscan_tests PRIVATE MSCAN_ENGINE_DIR="${CMAKE_CURRENT_SOURCE_DIR}")
  include(GoogleTest)
  gtest_discover_tests(mscan_tests)

  # ---- benchmarks -----------------------------------------------------------
  add_executable(mscan_bench bench/score_bench.cpp)
  target_link_libraries(mscan_bench PRIVATE mscan_core benchmark::benchmark_main)
  target_compile_definitions(mscan_bench PRIVATE MSCAN_ENGINE_DIR="${CMAKE_CURRENT_SOURCE_DIR}")
endif()

# ---- python module ----------------------------------------------------------
if(MSCAN_PYTHON)
  # Under pip (scikit-build-core) pybind11 comes from the build environment.
  # For a plain CMake build, ask the Python CMake found (the active venv, or
  # -DPython_EXECUTABLE=...) where pybind11's CMake files live.
  find_package(Python 3.11 REQUIRED COMPONENTS Interpreter Development.Module)
  if(NOT pybind11_DIR)
    execute_process(
      COMMAND "${Python_EXECUTABLE}" -m pybind11 --cmakedir
      OUTPUT_VARIABLE _pybind11_dir OUTPUT_STRIP_TRAILING_WHITESPACE
      RESULT_VARIABLE _pybind11_rc ERROR_QUIET)
    if(_pybind11_rc EQUAL 0)
      set(pybind11_DIR "${_pybind11_dir}")
    endif()
  endif()
  find_package(pybind11 CONFIG)
  if(NOT pybind11_FOUND)
    message(FATAL_ERROR
      "pybind11 not found for ${Python_EXECUTABLE}. Either `pip install pybind11` "
      "in that environment, point -DPython_EXECUTABLE at the project venv, or "
      "configure with -DMSCAN_PYTHON=OFF.")
  endif()

  pybind11_add_module(mscan_engine python/bindings.cpp)
  target_link_libraries(mscan_engine PRIVATE mscan_core)
  install(TARGETS mscan_engine LIBRARY DESTINATION .)   # top-level module for pip
endif()
