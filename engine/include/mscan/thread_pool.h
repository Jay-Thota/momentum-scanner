#pragma once
// Fixed-size thread pool. Written by hand on purpose (it is a resume talking
// point), so keep it small enough to explain on a whiteboard.
//
// Design questions to be able to answer:
//   - Why a single mutex + condition_variable queue is fine here (tasks are
//     coarse: one chunk of tickers each), and when you'd want work stealing.
//   - Shutdown: destructor sets stop_, notify_all, then jthreads join. Tasks
//     already queued finish; nothing new is accepted.
//   - Exceptions: submit() returns a std::future, so a throw inside a task
//     surfaces at .get() instead of killing a worker.
//   - The GIL: the pybind11 binding releases it (py::gil_scoped_release)
//     before calling into the pool, and workers never touch Python objects.

#include <condition_variable>
#include <cstddef>
#include <functional>
#include <future>
#include <mutex>
#include <queue>
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
  // fn(begin, end) must only write to indices in its own range.
  void parallel_for(std::size_t n,
                    const std::function<void(std::size_t, std::size_t)>& fn);

  std::size_t size() const { return workers_.size(); }

 private:
  void worker_loop(std::stop_token st);

  std::vector<std::jthread> workers_;
  std::queue<std::function<void()>> tasks_;
  std::mutex mu_;
  std::condition_variable_any cv_;
};

// TODO: submit() template body goes here (wrap in std::packaged_task,
// push under the lock, notify_one).

}  // namespace mscan
