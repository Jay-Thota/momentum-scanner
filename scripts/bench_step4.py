"""
python -m scripts.bench_step4 [--repeat 3]

Times Step 4 (layer 1 + layer 2 for every ticker) on the real universe from
engine/data/history_panel.npz:

    python      scorer._score_l1l2_python (the original loop)
    c++ total   cpp_engine.score_l1l2_cpp, single-threaded, which is
      stack       ohlcv_map -> (T, D) arrays (pandas/numpy)
      engine      mscan_engine.score_l1l2_batch

at the production lookback (~206 bars per ticker) and at full 4-year length.
Each figure is the best of --repeat runs. Prints a markdown table.
"""

import argparse
import logging
import time

import mscan_engine

from backend.scoring.cpp_engine import _FIELDS, engine_config, score_l1l2_cpp, stack_ohlcv
from backend.scoring.scorer import _score_l1l2_python
from backend.tests.validate_cpp_engine import PRODUCTION_SESSIONS, panel_frames

logging.disable(logging.WARNING)   # Python layers warn on every short history


def best_of(repeat: int, fn) -> float:
    times = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        times.append(time.perf_counter() - t0)
    return min(times)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    ap.add_argument("--repeat", type=int, default=3)
    args = ap.parse_args()

    print("| data | tickers | bars | python | c++ total | stack | engine | speedup (total) | speedup (engine) |")
    print("|---|---|---|---|---|---|---|---|---|")
    for label, sessions in (("production lookback", PRODUCTION_SESSIONS), ("full history", None)):
        frames = panel_frames(sessions)
        tickers = sorted(frames)
        bars = sum(len(df) for df in frames.values())
        arrays, dates = stack_ohlcv(tickers, frames)
        cfg = engine_config()

        py = best_of(args.repeat, lambda: _score_l1l2_python(tickers, frames))
        total = best_of(args.repeat, lambda: score_l1l2_cpp(tickers, frames))
        stack = best_of(args.repeat, lambda: stack_ohlcv(tickers, frames))
        engine = best_of(args.repeat, lambda: mscan_engine.score_l1l2_batch(
            tickers, *(arrays[f] for f in _FIELDS), cfg, dates=dates))

        print(f"| {label} | {len(tickers):,} | {bars:,} | {py:.2f} s | {total * 1000:.0f} ms "
              f"| {stack * 1000:.0f} ms | {engine * 1000:.0f} ms | {py / total:.1f}x | {py / engine:.0f}x |")


if __name__ == "__main__":
    main()
