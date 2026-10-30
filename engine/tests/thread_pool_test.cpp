#include <gtest/gtest.h>

#include <atomic>
#include <chrono>
#include <future>
#include <memory>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#include "mscan/thread_pool.h"

using mscan::ThreadPool;
using namespace std::chrono_literals;

TEST(ThreadPool, SubmitReturnsValue) {
  ThreadPool pool(4);
  auto f = pool.submit([] { return 6 * 7; });
  EXPECT_EQ(f.get(), 42);
}

TEST(ThreadPool, AcceptsMoveOnlyTasks) {
  // std::function can't hold this lambda (it owns a unique_ptr); submit can.
  ThreadPool pool(2);
  auto owned = std::make_unique<int>(7);
  auto f = pool.submit([p = std::move(owned)] { return *p * 2; });
  EXPECT_EQ(f.get(), 14);
}

TEST(ThreadPool, ExceptionPropagatesThroughFuture) {
  ThreadPool pool(2);
  auto bad = pool.submit([]() -> int { throw std::runtime_error("boom"); });
  try {
    bad.get();
    FAIL() << "expected the task's exception at get()";
  } catch (const std::runtime_error& e) {
    EXPECT_STREQ(e.what(), "boom");
  }
  // The worker survived and the pool still runs work
  EXPECT_EQ(pool.submit([] { return 1; }).get(), 1);
}

TEST(ThreadPool, ShutdownFinishesQueuedTasks) {
  std::atomic<int> done{0};
  std::promise<void> gate;
  std::shared_future<void> opened = gate.get_future().share();
  std::thread releaser;
  {
    ThreadPool pool(1);
    pool.submit([opened] { opened.wait(); });   // holds the only worker
    for (int i = 0; i < 100; ++i) pool.submit([&done] { ++done; });
    // The destructor starts while all 100 tasks are still queued behind the
    // blocked worker; the gate opens 50 ms later and the queue must drain.
    releaser = std::thread([&gate] {
      std::this_thread::sleep_for(50ms);
      gate.set_value();
    });
  }
  releaser.join();
  EXPECT_EQ(done.load(), 100);
}

TEST(ThreadPool, SubmitDuringShutdownThrows) {
  std::atomic<bool> rejected{false};
  std::promise<void> gate;
  std::shared_future<void> opened = gate.get_future().share();
  std::thread releaser;
  {
    ThreadPool pool(1);
    pool.submit([opened] { opened.wait(); });
    // Runs only after the destructor has begun, so its own submit must fail
    pool.submit([&pool, &rejected] {
      try {
        pool.submit([] {});
      } catch (const std::runtime_error&) {
        rejected = true;
      }
    });
    releaser = std::thread([&gate] {
      std::this_thread::sleep_for(50ms);
      gate.set_value();
    });
  }
  releaser.join();
  EXPECT_TRUE(rejected.load());
}

TEST(ThreadPool, ParallelForCoversEveryIndexExactlyOnce) {
  for (std::size_t threads : {1u, 3u, 8u}) {
    ThreadPool pool(threads);
    for (std::size_t n : {0u, 1u, 2u, 7u, 31u, 1000u, 10007u}) {
      std::vector<std::atomic<int>> hits(n);
      std::atomic<int> calls{0};
      pool.parallel_for(n, [&](std::size_t b, std::size_t e) {
        ASSERT_LT(b, e);
        ASSERT_LE(e, n);
        ++calls;
        for (std::size_t i = b; i < e; ++i) ++hits[i];
      });
      for (std::size_t i = 0; i < n; ++i) {
        ASSERT_EQ(hits[i].load(), 1) << "threads=" << threads << " n=" << n << " index " << i;
      }
      EXPECT_LE(static_cast<std::size_t>(calls.load()), std::min(n, threads * 4));
    }
  }
}

TEST(ThreadPool, ParallelForRethrowsAfterAllChunksFinish) {
  ThreadPool pool(4);
  const std::size_t n = 1000;
  std::vector<std::atomic<int>> hits(n);
  EXPECT_THROW(pool.parallel_for(n, [&](std::size_t b, std::size_t e) {
    if (b == 0) throw std::logic_error("first chunk fails");
    std::this_thread::sleep_for(1ms);   // later chunks are still running when it throws
    for (std::size_t i = b; i < e; ++i) ++hits[i];
  }), std::logic_error);
  // Every other chunk completed before parallel_for returned
  std::size_t first_chunk = 0;
  while (first_chunk < n && hits[first_chunk] == 0) ++first_chunk;
  for (std::size_t i = first_chunk; i < n; ++i) EXPECT_EQ(hits[i].load(), 1) << i;
  EXPECT_GT(first_chunk, 0u);
}

TEST(ThreadPool, ConcurrentSubmitters) {
  ThreadPool pool(4);
  std::atomic<int> total{0};
  std::vector<std::thread> producers;
  for (int p = 0; p < 4; ++p) {
    producers.emplace_back([&] {
      std::vector<std::future<void>> fs;
      for (int i = 0; i < 1000; ++i) fs.push_back(pool.submit([&total] { ++total; }));
      for (auto& f : fs) f.get();
    });
  }
  for (auto& t : producers) t.join();
  EXPECT_EQ(total.load(), 4000);
}
