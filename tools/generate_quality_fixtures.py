#!/usr/bin/env python3
"""Emit (sigma, tier) pairs from the Python classifier for the Kotlin test.

The handheld and the command post must describe the same estimate the same
way. Rather than copying the thresholds into the Kotlin test by hand -- where
they would drift the moment either side changed -- the expected values are
generated from the Python reference itself.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "edge-agent"))
from aura.ekf import classify_quality  # noqa: E402

# Boundaries, the measured drift cases, and a spread across the range.
SIGMAS = [
    0.0, 0.01, 0.028, 0.5, 0.74, 0.75, 0.7501, 0.9, 1.23, 1.59,
    2.99, 3.0, 3.0001, 5.0, 6.59, 9.99, 10.0, 10.0001,
    32.84, 93.24, 360.95, 589.11, 618.09, 3418.62, 79357.93,
]

parts = [f'{s}f to "{classify_quality(s)}"' for s in SIGMAS]
print("CASES=" + ", ".join(parts))
