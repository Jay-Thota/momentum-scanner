"""
python -m scripts.dump_rounding_golden [--out engine/tests/golden/rounding.csv]

Writes Python and NumPy rounding results for the C++ rounding helpers
(engine/include/mscan/pyround.h) to test against.

Columns: x, py2, py3, np2, np3 where pyN = round(float(x), N) and
npN = round(numpy.float64(x), N). Inputs mix decimal halfway cases (2.675,
1.0005, ...), values like the ratios the layers report, random magnitudes,
negatives and signed zero. Floats are written as repr (exact round trip).
"""

import argparse
import csv
from pathlib import Path

import numpy as np

SEED = 20261029


def inputs() -> list[float]:
    rng = np.random.default_rng(SEED)
    xs: list[float] = []
    # Decimal halfway points at 2 and 3 places: k/200 + 1/400 and k/2000 + 1/4000
    xs += [k / 200 + 1 / 400 for k in range(-400, 2000)]
    xs += [k / 2000 + 1 / 4000 for k in range(-400, 4000)]
    xs += [2.675, 1.005, 0.125, 0.375, 1.0005, 2.0045, 0.0005, -0.0005, 0.0, -0.0]
    xs += list(rng.uniform(0, 10, 4000))          # ratios like vol_ratio
    xs += list(rng.uniform(0, 100, 2000))         # ADX / DI
    xs += list(rng.uniform(-50, 50, 2000))        # ROC
    xs += list(10.0 ** rng.uniform(-6, 9, 2000))  # wide magnitudes
    return [float(x) for x in xs]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    ap.add_argument("--out", type=Path, default=Path("engine/tests/golden/rounding.csv"))
    args = ap.parse_args()

    rows = 0
    with args.out.open("w", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["x", "py2", "py3", "np2", "np3"])
        for x in inputs():
            f = np.float64(x)
            w.writerow([repr(x), repr(round(x, 2)), repr(round(x, 3)),
                        repr(float(round(f, 2))), repr(float(round(f, 3)))])
            rows += 1
    print(f"Wrote {rows} rows to {args.out}")


if __name__ == "__main__":
    main()
