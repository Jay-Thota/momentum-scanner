"""
python -m scripts.bench_step4 [--repeat 3]

End-to-end timing of the scan from the Alpaca bars response through Step 4
(layer 1 + layer 2), on the real universe from engine/data/history_panel.npz
at the production lookback. Network time is excluded: every path starts from
the same decoded JSON response.

    python       alpaca-py Bar models -> DataFrames -> Step 2 loop -> Python layers
                 (the scan with USE_CPP_ENGINE off)
    c++ (frames) the same, then DataFrames -> arrays -> engine (phase 3)
    c++ (arrays) raw JSON -> OhlcvBatch -> vectorized Step 2 -> engine (phase 4)

The arrays path still builds DataFrames later, for tickers a later step reads
(the Step 5 survivors); that cost is reported separately. Each figure is the
best of --repeat runs, single-threaded. Prints markdown tables.
"""

import argparse
import logging
import time

from alpaca.data.models import Bar

from backend.data.fetcher_price import bars_to_frame
from backend.data.ohlcv_batch import LazyOhlcvMap, OhlcvBatch
from backend.scoring.cpp_engine import score_l1l2_cpp
from backend.scoring.scorer import _score_l1l2_python, l0_price_volume_pass
from backend.tests.validate_cpp_engine import PRODUCTION_SESSIONS, alpaca_raw_bars, panel_frames

logging.disable(logging.WARNING)   # Python layers warn on every short history


def best_of(repeat: int, fn):
    best, out = float("inf"), None
    for _ in range(repeat):
        t0 = time.perf_counter()
        out = fn()
        best = min(best, time.perf_counter() - t0)
    return best, out


def survivors(scores: dict[str, dict]) -> list[str]:
    """Step 5: drop L1 = 0 and L2 < 0.5."""
    return [t for t, s in scores.items()
            if not (s["l1"]["score"] == 0.0 and s["l2"].get("total", 0.0) < 0.5)]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    ap.add_argument("--repeat", type=int, default=3)
    args = ap.parse_args()
    r = args.repeat

    raw = alpaca_raw_bars(panel_frames(PRODUCTION_SESSIONS))
    tickers = list(raw)
    bars_total = sum(len(v) for v in raw.values())

    # Stages shared by the two DataFrame paths
    t_models, models = best_of(r, lambda: {t: [Bar(t, d) for d in rows] for t, rows in raw.items()})
    t_frames, frames = best_of(r, lambda: {t: bars_to_frame(b) for t, b in models.items()})
    t_step2_loop, pv = best_of(r, lambda: l0_price_volume_pass(tickers, frames))
    t_py_layers, py_scores = best_of(r, lambda: _score_l1l2_python(pv, frames))
    t_cpp_frames, _ = best_of(r, lambda: score_l1l2_cpp(pv, frames))

    # Arrays path
    t_batch, batch = best_of(r, lambda: OhlcvBatch.from_raw_bars(raw, tickers))
    lazy = LazyOhlcvMap(batch)
    t_step2_vec, pv2 = best_of(r, lambda: l0_price_volume_pass(tickers, lazy))
    t_cpp_arrays, cpp_scores = best_of(r, lambda: score_l1l2_cpp(pv2, lazy))
    assert pv2 == pv
    surv = survivors(cpp_scores)
    t_surv_frames, _ = best_of(r, lambda: [batch.frame(t) for t in surv])

    py_total = t_models + t_frames + t_step2_loop + t_py_layers
    c3_total = t_models + t_frames + t_step2_loop + t_cpp_frames
    c4_total = t_batch + t_step2_vec + t_cpp_arrays

    ms = lambda s: f"{s * 1000:,.0f} ms"
    print(f"{len(tickers):,} tickers, {bars_total:,} bars, {len(pv):,} pass Step 2, "
          f"{len(surv):,} survive Step 5\n")
    print("| stage | python | c++ (frames, phase 3) | c++ (arrays, phase 4) |")
    print("|---|---|---|---|")
    print(f"| JSON -> alpaca-py Bar models | {ms(t_models)} | {ms(t_models)} | - |")
    print(f"| Bar models -> DataFrames | {ms(t_frames)} | {ms(t_frames)} | - |")
    print(f"| raw JSON -> OhlcvBatch arrays | - | - | {ms(t_batch)} |")
    print(f"| Step 2 price/volume gate | {ms(t_step2_loop)} | {ms(t_step2_loop)} | {ms(t_step2_vec)} |")
    print(f"| Step 4 layers 1 + 2 | {ms(t_py_layers)} | {ms(t_cpp_frames)} | {ms(t_cpp_arrays)} |")
    print(f"| **total, response -> Step 4** | **{ms(py_total)}** | **{ms(c3_total)}** "
          f"| **{ms(c4_total)}** |")
    print(f"| speedup vs python | 1.0x | {py_total / c3_total:.1f}x | {py_total / c4_total:.1f}x |")
    print(f"\nLater, arrays path only: DataFrames for the {len(surv):,} Step 5 survivors "
          f"{ms(t_surv_frames)} (the other paths built all {len(tickers):,} up front).")


if __name__ == "__main__":
    main()
