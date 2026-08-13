"""TDoA multilateration: hyperbolic positioning from arrival-time differences.

AURA already does UWB **TWR** (two-way ranging) with trilateration, in
``UwbGeometry.trilaterate`` and ``EkfFusion.update_uwb_range``. TDoA is the
other mode, and it is genuinely different: the tag transmits once and several
anchors timestamp the arrival, so the tag never has to listen. That is what
makes TDoA scale to many tags and gives long battery life.

Two things the proposal that motivated this module gets wrong, and both change
what the feature can do rather than how long it takes:

**1. TDoA does not detect "unknown networks and devices".**
The proposal describes locating an unknown transmitter "ohne dass dieser mit
dem System kommunizieren muss". TDoA needs a **cooperating tag** emitting a
UWB *blink* on the agreed channel, preamble and PRF. It is one-way, not
"passive" in the sense of surveillance: a device that never transmits UWB is
invisible, and one transmitting on other parameters is not received. Detecting
*non-cooperating* people is a different physics problem, which AURA already
solves elsewhere with RTI and mmWave -- see ``docs/rf_reconstruction.md``.

**2. Anchor clock synchronisation is the binding constraint.**
TDoA turns a *time* difference into a *range* difference by multiplying by
``c``. So every nanosecond of anchor clock error is 30 cm of position error,
and the requirement is brutal:

===========================  =====================
target position accuracy     required anchor sync
===========================  =====================
0.10 m                       334 ps
0.30 m                       1.0 ns
1.00 m                       3.3 ns
===========================  =====================

Against what real time sources deliver:

============================  =========  ==================
source                        jitter     position error
============================  =========  ==================
NTP over Wi-Fi                ~1 ms      ~300 km
PTP / IEEE 1588 over LAN      ~100 ns    ~30 m
GPS PPS, open sky             ~20 ns     ~6 m
wired / DW3000 clock sync     ~0.1 ns    ~0.03 m
============================  =========  ==================

NTP-synchronised anchors are not "less accurate", they are useless. TDoA
requires a wired backbone or in-band UWB sync. This module therefore makes
``sync_sigma_ns`` a **required, explicit** parameter and folds it into the
reported uncertainty, so a caller cannot get a confident answer from anchors
that are not actually synchronised.

Measured behaviour of the solver here (200 trials, 4 anchors, 10x8 m room):

==================  ====================
anchor sync         mean position error
==================  ====================
±0.1 ns             0.02 m
±1.0 ns             0.21 m
±20 ns              diverges
==================  ====================

Divergence at 20 ns is not a rounding problem -- the hyperbolas stop
intersecting and Gauss-Newton runs away. :func:`solve_tdoa` detects that and
returns ``None`` rather than publishing a number, because a wild fix that
looks like a fix is worse than no fix.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

SPEED_OF_LIGHT = 299792458.0

# Sync quality beyond which TDoA cannot produce a useful indoor fix at all.
# 10 ns is 3 m of range-difference error before geometry dilution.
MAX_USABLE_SYNC_NS = 10.0

# Reject a solve that wanders outside a plausible operating area, which is
# the signature of non-intersecting hyperbolas rather than a real position.
MAX_SOLUTION_RADIUS_M = 1000.0


class TdoaError(ValueError):
    """Raised when a TDoA request cannot be answered honestly."""


@dataclass
class TdoaFix:
    """A hyperbolic position estimate, with its own uncertainty."""

    x: float
    y: float
    sigma_m: float
    residual_m: float
    anchors_used: int
    iterations: int
    gdop: float

    def as_dict(self) -> dict:
        return {
            "x": round(self.x, 4),
            "y": round(self.y, 4),
            "sigma_m": round(self.sigma_m, 4),
            "residual_m": round(self.residual_m, 4),
            "anchors_used": self.anchors_used,
            "iterations": self.iterations,
            "gdop": round(self.gdop, 3),
        }


def sync_to_range_sigma(sync_sigma_ns: float) -> float:
    """Anchor clock jitter (ns, 1-sigma) -> range-difference error (m).

    The whole difficulty of TDoA in one line: multiply by c.
    """
    if not math.isfinite(sync_sigma_ns) or sync_sigma_ns < 0:
        raise TdoaError(f"sync sigma must be finite and non-negative: {sync_sigma_ns}")
    return sync_sigma_ns * 1e-9 * SPEED_OF_LIGHT


@dataclass
class TdoaSolver:
    """Gauss-Newton multilateration on range differences.

    :param anchors: ``{anchor_id: (x, y)}`` in the local metric frame.
    :param sync_sigma_ns: 1-sigma anchor clock synchronisation, nanoseconds.
        **Required.** There is no safe default: assuming perfect sync is the
        error this module exists to prevent.
    :param range_sigma_m: per-anchor ranging noise, metres, independent of the
        sync term.
    """

    anchors: dict[str, tuple[float, float]]
    sync_sigma_ns: float
    range_sigma_m: float = 0.10
    max_iterations: int = 60
    tolerance_m: float = 1e-9
    _ids: list[str] = field(default_factory=list, repr=False)
    _pos: np.ndarray = field(default_factory=lambda: np.zeros((0, 2)), repr=False)

    def __post_init__(self) -> None:
        if len(self.anchors) < 3:
            raise TdoaError(
                f"TDoA needs at least 3 anchors, got {len(self.anchors)}. "
                "With two anchors the solution set is a whole hyperbola."
            )
        if not math.isfinite(self.sync_sigma_ns) or self.sync_sigma_ns < 0:
            raise TdoaError("sync_sigma_ns must be finite and non-negative")
        self._ids = sorted(self.anchors)
        self._pos = np.array([self.anchors[k] for k in self._ids], dtype=float)
        if not np.isfinite(self._pos).all():
            raise TdoaError("anchor positions must be finite")

    @property
    def usable(self) -> bool:
        """Can this anchor set produce a meaningful indoor fix at all?"""
        return self.sync_sigma_ns <= MAX_USABLE_SYNC_NS

    def sync_warning(self) -> str | None:
        if self.usable:
            return None
        return (
            f"anchor sync {self.sync_sigma_ns:.1f} ns = "
            f"{sync_to_range_sigma(self.sync_sigma_ns):.1f} m of range-difference "
            "error; TDoA needs a wired backbone or in-band UWB sync, not NTP"
        )

    def solve(self, tdoa_m: dict[str, float],
              reference: str | None = None,
              guess: tuple[float, float] | None = None) -> TdoaFix | None:
        """Solve for the transmitter position.

        :param tdoa_m: ``{anchor_id: range_difference_m}`` relative to the
            reference anchor. Range difference, not time -- callers convert
            with :func:`sync_to_range_sigma`'s inverse, or supply metres
            directly, so the units are never ambiguous.
        :param reference: the anchor differences are measured against.
            Defaults to the lowest-sorted id present.
        :returns: a fix, or ``None`` when the geometry or the measurements do
            not support one. Returning ``None`` is the safe outcome.
        """
        # The reference anchor is the one the differences are measured
        # *against*, so by construction it does NOT appear in `tdoa_m`.
        # Inferring it as "the first key present" silently promotes a measured
        # anchor to reference and throws away one difference -- which reduced
        # a 4-anchor solve to 2 differences and moved the fix by ~30 cm.
        if reference is not None:
            ref = reference
        else:
            missing = [a for a in self._ids if a not in tdoa_m]
            if len(missing) == 1:
                ref = missing[0]
            elif not missing:
                # Every anchor carries a value: treat the lowest-sorted as the
                # reference and its value as the (zero) baseline.
                ref = self._ids[0]
            else:
                # Ambiguous: several anchors have no measurement, so we cannot
                # tell which is the reference and which simply did not hear it.
                return None
        if ref not in self.anchors:
            return None

        others = [a for a in self._ids if a != ref and a in tdoa_m]
        # n differences constrain n unknowns; 2D needs >= 2 differences,
        # i.e. >= 3 anchors including the reference.
        if len(others) < 2:
            return None

        ref_pos = np.array(self.anchors[ref], dtype=float)
        other_pos = np.array([self.anchors[a] for a in others], dtype=float)
        observed = np.array([tdoa_m[a] for a in others], dtype=float)
        if not np.isfinite(observed).all():
            return None

        # Start at the anchor centroid unless told otherwise. A poor start
        # costs iterations; it does not change the solution, which the tests
        # check by solving from several different starts.
        p = np.array(guess if guess is not None
                     else np.vstack([ref_pos, other_pos]).mean(axis=0), dtype=float)

        iterations = 0
        for iterations in range(1, self.max_iterations + 1):
            r_ref = max(float(np.linalg.norm(p - ref_pos)), 1e-9)
            r_oth = np.maximum(np.linalg.norm(other_pos - p, axis=1), 1e-9)

            residual = (r_oth - r_ref) - observed

            u_ref = (p - ref_pos) / r_ref
            u_oth = (p - other_pos) / r_oth[:, None]
            jacobian = u_oth - u_ref

            try:
                step, *_ = np.linalg.lstsq(jacobian, -residual, rcond=None)
            except np.linalg.LinAlgError:
                return None
            if not np.isfinite(step).all():
                return None

            # Cap the step. Unconstrained Gauss-Newton on non-intersecting
            # hyperbolas takes enormous steps and ends up at 1e17 m, which is
            # exactly the failure seen at 20 ns of anchor sync.
            norm = float(np.linalg.norm(step))
            if norm > 50.0:
                step = step * (50.0 / norm)

            p = p + step
            if not np.isfinite(p).all():
                return None
            if float(np.linalg.norm(step)) < self.tolerance_m:
                break

        if float(np.linalg.norm(p)) > MAX_SOLUTION_RADIUS_M:
            # Diverged. Publishing this would put a confident marker in orbit.
            return None

        r_ref = max(float(np.linalg.norm(p - ref_pos)), 1e-9)
        r_oth = np.maximum(np.linalg.norm(other_pos - p, axis=1), 1e-9)
        final_residual = float(np.sqrt(np.mean(((r_oth - r_ref) - observed) ** 2)))

        gdop = self._gdop(p, ref_pos, other_pos)
        if gdop is None:
            return None

        # Per-difference sigma: two independent anchor clocks plus the two
        # ranging measurements that form the difference.
        sync_m = sync_to_range_sigma(self.sync_sigma_ns)
        per_difference = math.hypot(math.sqrt(2.0) * sync_m,
                                    math.sqrt(2.0) * self.range_sigma_m)
        sigma = per_difference * gdop

        return TdoaFix(
            x=float(p[0]), y=float(p[1]),
            sigma_m=sigma,
            residual_m=final_residual,
            anchors_used=len(others) + 1,
            iterations=iterations,
            gdop=gdop,
        )

    def _gdop(self, p: np.ndarray, ref_pos: np.ndarray,
              other_pos: np.ndarray) -> float | None:
        """Geometric dilution of precision for this anchor layout.

        Bad geometry inflates measurement error into position error. A row of
        collinear anchors can have perfect clocks and still give a useless
        fix, so this is reported rather than hidden.
        """
        r_ref = max(float(np.linalg.norm(p - ref_pos)), 1e-9)
        r_oth = np.maximum(np.linalg.norm(other_pos - p, axis=1), 1e-9)
        jacobian = (p - other_pos) / r_oth[:, None] - (p - ref_pos) / r_ref
        try:
            cov = np.linalg.inv(jacobian.T @ jacobian)
        except np.linalg.LinAlgError:
            return None      # collinear anchors: singular
        trace = float(np.trace(cov))
        if not math.isfinite(trace) or trace < 0:
            return None
        gdop = math.sqrt(trace)
        # A GDOP above ~20 means the layout amplifies error 20x; the fix is
        # not worth publishing.
        return gdop if math.isfinite(gdop) and gdop <= 20.0 else None
