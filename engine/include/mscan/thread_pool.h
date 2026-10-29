#pragma once
// Fixed-size thread pool. Written by hand on purpose (it is a resume talking
// point), so keep it small enough to explain on a whiteboard.
//
// Design questions to be able to answer:
//   - Why a single mutex + condition_variable queue is fine here (tasks are
//     coarse: one chunk of tickers each), and when you'd want work stealing.
//   - Shutdown: destructor sets stopping_, requests stop on every jthread
//     (which wakes the condition variable), then joins. Tasks already queued
//     finish; submit() during shutdown throws.
//   - Exceptions: submit() returns a std::future, so a throw inside a task
//     surfaces at .get() instead of killing a worker.
//   - The GIL: the pybind11 binding releases it (py::gil_scoped_release)
//     before calling into the pool, and workers never touch Python objects.

#include <condition_variable>
#include <cstddef>
#include <functional>
#include <future>
#include <memory>
#include <mutex>
#include <queue>
#include <stdexcept>
#include <stop_token>
#include <thread>
#include <type_traits>
#include <vector>

namespace mscan {

class ThreadPool {
 public:
  explicit ThreadPool(std::size_t num_threads = std::thread::hardware_concurrency());
  ~ThreadPool();

  ThreadPool(const ThreadPool&) = delete;
  ThreadPool& operator=(const ThreadPool&) = delete;

  template <class F>
  auto submit(F&& f) -> std::future<std::invoke_result_t<F>>;

  // Splits [0, n) into ~4 chunks per thread and blocks until all are done.
  // fn(begin, end) must only write to indices in its own range. If a chunk
  // throws, every chunk still finishes before the first exception is
  // rethrown here. Don't call from inside a pool task: the caller's worker
  // would block waiting on chunks that may need that very worker.
  void parallel_for(std::size_t n,
                    const std::function<void(std::size_t, std::size_t)>& fn);

  std::size_t size() const { return workers_.size(); }

 private:
  void worker_loop(std::stop_token st);

  std::vector<std::jthread> workers_;
  std::queue<std::function<void()>> tasks_;
  std::mutex mu_;                     // guards tasks_ and stopping_
  std::condition_variable_any cv_;    // _any: its wait() can also wake on a stop_token
  bool stopping_ = false;
};

template <class F>
auto ThreadPool::submit(F&& f) -> std::future<std::invoke_result_t<F>> {
  using R = std::invoke_result_t<F>;
  // packaged_task runs f and stores its result or exception in the future.
  // It is move-only but std::function needs a copyable callable, so the queue
  // holds a small lambda sharing ownership of the task.
  auto task = std::make_shared<std::packaged_task<R()>>(std::forward<F>(f));
  std::future<R> result = task->get_future();
  {
    std::lock_guard lock(mu_);
    if (stopping_) throw std::runtime_error("ThreadPool::submit called during shutdown");
    tasks_.emplace([task] { (*task)(); });
  }
  cv_.notify_one();   // outside the lock, so the woken worker can take it at once
  return result;
}

}  // namespace mscan
