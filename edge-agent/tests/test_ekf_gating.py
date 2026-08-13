"""Measurement gating: rejecting bad data without starving the filter.

Two failure modes, pulling in opposite directions, and the tests exist to hold
both down at once:

* Accept everything, and one corrupted serial line moves the operator 8 km
  while the filter reports ``quality=good``.
* Gate too eagerly, and an overconfident filter rejects the very measurements
  that would correct it -- converging confidently to the wrong answer.

The second is the subtler one and cost a round trip to find: a global
consecutive-rejection counter looks fine while two of four anchors are locked
out permanently, because the other two keep resetting it.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from aura.ekf import (
    EkfConfig,
    ExtendedKalmanFilter,
    mahalanobis_gate,
)

ANCHORS = [(0.0, 0.0, 2.4), (12.0, 0.0, 2.4), (12.0, 9.0, 2.4), (0.0, 9.0, 2.4)]
TRUTH = np.array([6.0, 4.0, 1.4])


def _converged_filter(iterations: int = 200) -> ExtendedKalmanFilter:
    ekf = ExtendedKalmanFilter()
    ekf.x[0], ekf.x[1], ekf.x[2] = TRUTH
    for _ in range(iterations):
        for anchor in ANCHORS:
            ekf.update_uwb_range(
                anchor, float(np.linalg.norm(TRUTH - np.asarray(anchor))), sigma=0.05
            )
    return ekf


# ----------------------------------------------------------------------
# the gate function itself
# ----------------------------------------------------------------------


def test_gate_accepts_a_plausible_innovation():
    assert mahalanobis_gate([0.1], np.array([[0.12 ** 2]]), 16.0)


def test_gate_rejects_a_four_sigma_innovation():
    # threshold 16 == 4 sigma for one degree of freedom
    assert mahalanobis_gate([0.47], np.array([[0.12 ** 2]]), 16.0)
    assert not mahalanobis_gate([0.60], np.array([[0.12 ** 2]]), 16.0)


def test_gate_scales_with_the_sensor_noise():
    """The same absolute error is fine for BLE and absurd for UWB."""
    innovation = [2.0]
    assert not mahalanobis_gate(innovation, np.array([[0.12 ** 2]]), 16.0)
    assert mahalanobis_gate(innovation, np.array([[1.40 ** 2]]), 16.0)


def test_gate_rejects_non_finite_input():
    for bad in (float("nan"), float("inf"), float("-inf")):
        assert not mahalanobis_gate([bad], np.array([[0.01]]), 16.0)
    assert not mahalanobis_gate([1.0], np.array([[float("nan")]]), 16.0)


def test_singular_covariance_accepts_rather_than_starves():
    """If we cannot say how surprising a measurement is, do not gate it out."""
    assert mahalanobis_gate([1.0], np.array([[0.0]]), 16.0)


# ----------------------------------------------------------------------
# non-finite measurements must never reach the state
# ----------------------------------------------------------------------


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_range_cannot_poison_the_state(bad):
    """One NaN used to NaN the whole state vector, permanently."""
    ekf = _converged_filter(50)
    before = ekf.position.copy()
    ekf.update_uwb_range(ANCHORS[0], bad, sigma=0.05)
    assert np.all(np.isfinite(ekf.x)), "state went non-finite"
    assert np.all(np.isfinite(ekf.P)), "covariance went non-finite"
    assert np.allclose(ekf.position, before)
    assert ekf.rejected >= 1


def test_non_finite_altitude_is_also_refused():
    ekf = _converged_filter(50)
    ekf.update_altitude(float("nan"), sigma=0.1)
    assert np.all(np.isfinite(ekf.x))


def test_a_nan_does_not_crash_the_update():
    """It used to raise ValueError: math domain error out of wrap_pi."""
    ekf = _converged_filter(20)
    ekf.update_uwb_range(ANCHORS[0], float("inf"), sigma=0.05)  # must not raise


# ----------------------------------------------------------------------
# outlier rejection
# ----------------------------------------------------------------------


def test_a_single_wild_range_does_not_move_the_estimate():
    """Measured before the gate: +200 m moved the fix 8 m, permanently."""
    ekf = _converged_filter()
    before = ekf.position[:2].copy()
    true_range = float(np.linalg.norm(TRUTH - np.asarray(ANCHORS[0])))
    ekf.update_uwb_range(ANCHORS[0], true_range + 200.0, sigma=0.05)
    assert np.linalg.norm(ekf.position[:2] - before) < 0.01
    assert ekf.rejected >= 1


def test_rejected_updates_are_counted_and_exposed():
    ekf = _converged_filter()
    true_range = float(np.linalg.norm(TRUTH - np.asarray(ANCHORS[0])))
    ekf.update_uwb_range(ANCHORS[0], true_range + 500.0, sigma=0.05)
    snap = ekf.snapshot(0.0).as_dict()
    assert snap["rejected"] >= 1


def test_an_honest_measurement_still_gets_through():
    ekf = _converged_filter()
    accepted_before = ekf.updates
    for anchor in ANCHORS:
        ekf.update_uwb_range(
            anchor, float(np.linalg.norm(TRUTH - np.asarray(anchor))), sigma=0.05
        )
    assert ekf.updates == accepted_before + len(ANCHORS)


# ----------------------------------------------------------------------
# the starvation trap
# ----------------------------------------------------------------------


def test_cold_start_still_converges_despite_gating():
    """The regression that caught the first attempt at this gate.

    Starting 5.8 m from truth, the filter grows confident faster than it grows
    accurate. With a naive gate it rejected 359 updates in a row and settled
    0.36 m out. It must still converge.
    """
    ekf = ExtendedKalmanFilter()
    ekf.x[0], ekf.x[1], ekf.x[2] = 1.0, 1.0, 1.4
    for _ in range(120):
        for anchor in ANCHORS:
            ekf.update_uwb_range(
                anchor, float(np.linalg.norm(TRUTH - np.asarray(anchor))), sigma=0.05
            )
        ekf.update_altitude(1.4, sigma=0.1)
    assert np.linalg.norm(ekf.position[:2] - TRUTH[:2]) < 0.15


def test_rejection_streaks_are_tracked_per_source_not_globally():
    """A global counter hides a permanently locked-out anchor.

    Two of four anchors can be gated away forever while the other two keep
    resetting a shared counter, so it never reaches the escape threshold.
    """
    ekf = ExtendedKalmanFilter()
    ekf.x[0], ekf.x[1], ekf.x[2] = 1.0, 1.0, 1.4
    for _ in range(30):
        for anchor in ANCHORS:
            ekf.update_uwb_range(
                anchor, float(np.linalg.norm(TRUTH - np.asarray(anchor))), sigma=0.05
            )
    assert isinstance(ekf._reject_streaks, dict)
    assert len(ekf._reject_streaks) > 1, "streaks are not being separated by source"


def test_persistent_rejection_eventually_lets_one_through():
    """After the escape threshold the filter distrusts itself, not the sensor."""
    cfg = EkfConfig(max_consecutive_rejects=3)
    ekf = ExtendedKalmanFilter(cfg)
    ekf.x[0], ekf.x[1], ekf.x[2] = TRUTH
    for _ in range(100):
        for anchor in ANCHORS:
            ekf.update_uwb_range(
                anchor, float(np.linalg.norm(TRUTH - np.asarray(anchor))), sigma=0.05
            )

    # A sensor that has genuinely moved: every reading now offset the same way.
    offset_range = float(np.linalg.norm(TRUTH - np.asarray(ANCHORS[0]))) + 3.0
    accepted = 0
    for _ in range(20):
        before = ekf.updates
        ekf.update_uwb_range(ANCHORS[0], offset_range, sigma=0.05)
        if ekf.updates > before:
            accepted += 1
    assert accepted >= 1, "filter locked itself out permanently"


def test_recovery_update_nudges_rather_than_yanks():
    """The escape must not become a bypass for genuine outliers."""
    cfg = EkfConfig(max_consecutive_rejects=2, reject_recovery_inflation=100.0)
    ekf = ExtendedKalmanFilter(cfg)
    ekf.x[0], ekf.x[1], ekf.x[2] = TRUTH
    for _ in range(100):
        for anchor in ANCHORS:
            ekf.update_uwb_range(
                anchor, float(np.linalg.norm(TRUTH - np.asarray(anchor))), sigma=0.05
            )
    before = ekf.position[:2].copy()
    wild = float(np.linalg.norm(TRUTH - np.asarray(ANCHORS[0]))) + 1000.0
    for _ in range(10):
        ekf.update_uwb_range(ANCHORS[0], wild, sigma=0.05)
    moved = np.linalg.norm(ekf.position[:2] - before)
    assert moved < 5.0, f"recovery let a 1000 m outlier move the fix {moved:.1f} m"


def test_gate_threshold_is_configurable():
    """Starting away from truth, so the innovations are large enough to gate.

    Started *at* truth the innovations are ~0 and even a threshold of 1.0
    rejects nothing -- which says nothing about the threshold.
    """
    tight = ExtendedKalmanFilter(EkfConfig(gate_threshold=1.0))
    loose = ExtendedKalmanFilter(EkfConfig(gate_threshold=1e6))
    for ekf in (tight, loose):
        ekf.x[0], ekf.x[1], ekf.x[2] = 1.0, 1.0, 1.4
        for _ in range(50):
            for anchor in ANCHORS:
                ekf.update_uwb_range(
                    anchor, float(np.linalg.norm(TRUTH - np.asarray(anchor))), sigma=0.05
                )
    assert tight.rejected > loose.rejected
    assert loose.rejected == 0


# ----------------------------------------------------------------------
# no false positives in normal operation
# ----------------------------------------------------------------------


def test_gating_does_not_degrade_a_healthy_run():
    """900 ticks of the full pipeline: accuracy must match the old baseline.

    Project baseline: 600-tick planar error mean 0.157 m, max 0.194 m.
    """
    from aura.config import AgentConfig
    from aura.fusion import FusionPipeline

    pipe = FusionPipeline(AgentConfig(simulate=True, db_path=":memory:"))
    for driver in pipe.drivers:
        driver.open()

    errors = []
    for i in range(900):
        pipe.tick()
        if i > 300 and i % 50 == 0:
            truth = pipe.imu.ground_truth()
            if truth is not None:
                errors.append(
                    math.dist(pipe.state()["ekf"]["position"][:2], truth[:2])
                )

    assert errors, "no ground truth available"
    assert sum(errors) / len(errors) < 0.25
    ekf = pipe.state()["ekf"]
    assert ekf["quality"] == "good"
    # A healthy simulated run should not be tripping the gate at all.
    assert ekf["rejected"] <= ekf["updates"] * 0.01


def test_non_finite_guard_holds_even_with_the_chi_square_gate_disabled():
    """The two guards overlap; this isolates the first one.

    `mahalanobis_gate` refuses non-finite input independently of the
    threshold, so the explicit finite-check in `_update` is genuinely
    redundant today: deleting it leaves every test in this file green. That is
    recorded honestly in the `_update` docstring rather than papered over.

    What this test actually pins is the *invariant* -- no non-finite
    measurement may reach the state -- regardless of which of the two guards
    happens to enforce it.
    """
    ekf = ExtendedKalmanFilter(EkfConfig(gate_threshold=1e12))
    ekf.x[0], ekf.x[1], ekf.x[2] = TRUTH
    for _ in range(50):
        for anchor in ANCHORS:
            ekf.update_uwb_range(
                anchor, float(np.linalg.norm(TRUTH - np.asarray(anchor))), sigma=0.05
            )
    before = ekf.position.copy()

    for bad in (float("nan"), float("inf")):
        ekf.update_uwb_range(ANCHORS[0], bad, sigma=0.05)
        assert np.all(np.isfinite(ekf.x)), "state poisoned with the gate wide open"
        assert np.all(np.isfinite(ekf.P))
    assert np.allclose(ekf.position, before)


def test_non_finite_noise_or_jacobian_is_refused():
    """R and H are never inspected by the chi-square gate."""
    ekf = ExtendedKalmanFilter()
    ekf.x[0], ekf.x[1], ekf.x[2] = TRUTH
    for _ in range(20):
        ekf.update_uwb_range(ANCHORS[0], 7.28, sigma=0.05)
    before = ekf.position.copy()
    ekf.update_uwb_range(ANCHORS[0], 7.28, sigma=float("nan"))
    assert np.all(np.isfinite(ekf.x))
    assert np.allclose(ekf.position, before)
