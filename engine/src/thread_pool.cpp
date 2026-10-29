#include "mscan/thread_pool.h"

#include <algorithm>

namespace mscan {

ThreadPool::ThreadPool(std::size_t num_threads) {
  // hardware_concurrency() may return 0 when it can't tell
  if (num_threads == 0) num_threads = 1;
  workers_.reserve(num_threads);
  for (std::size_t i = 0; i < num_threads; ++i) {
    // jthread passes its own stop_token as the first argument
    workers_.emplace_back([this](std::stop_token st) { worker_loop(st); });
  }
}

ThreadPool::~ThreadPool() {
  {
    std::lock_guard lock(mu_);
    stopping_ = true;   // submit() refuses new work from here on
  }
  for (auto& w : workers_) w.request_stop();   // wakes any worker blocked in cv_.wait
  // Join explicitly, here in the destructor body. Members are destroyed in
  // reverse declaration order, so cv_, mu_ and tasks_ would be gone before
  // workers_ if we left the joining to the implicit member destructors.
  workers_.clear();
}

void ThreadPool::worker_loop(std::stop_token st) {
  for (;;) {
    std::function<void()> task;
    {
      std::unique_lock lock(mu_);
      // Sleeps until there is work or stop is requested. Returns at once
      // while tasks remain, so a stopping pool still drains its queue.
      cv_.wait(lock, st, [this] { return !tasks_.empty(); });
      if (tasks_.empty()) return;   // stop requested and nothing left
      task = std::move(tasks_.front());
      tasks_.pop();
    }
    task();   // outside the lock; packaged_task catches anything it throws
  }
}

void ThreadPool::parallel_for(std::size_t n,
                              const std::function<void(std::size_t, std::size_t)>& fn) {
  if (n == 0) return;
  // ~4 chunks per thread: enough slack that a slow chunk (a ticker with a
  // long history) doesn't leave the other threads idle at the end.
  const std::size_t chunks = std::min(n, size() * 4);
  const std::size_t base = n / chunks, extra = n % chunks;

  std::vector<std::future<void>> done;
  done.reserve(chunks);
  std::size_t begin = 0;
  for (std::size_t c = 0; c < chunks; ++c) {
    const std::size_t end = begin + base + (c < extra ? 1 : 0);
    done.push_back(submit([&fn, begin, end] { fn(begin, end); }));
    begin = end;
  }
  // Wait for every chunk before rethrowing: the chunks hold a reference to
  // fn, which must outlive them.
  for (auto& f : done) f.wait();
  for (auto& f : done) f.get();   // rethrows the first chunk's exception
}

}  // namespace mscan
