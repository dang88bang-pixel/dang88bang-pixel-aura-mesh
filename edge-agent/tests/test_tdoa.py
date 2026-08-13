"""TDoA multilateration.

The claims worth pinning are not "it converges" but:
  * anchor clock sync is the binding constraint, and it is reported;
  * bad geometry and bad sync produce *no fix* rather than a wild one;
  * the reported sigma reflects both, so nothing downstream is misled.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from aura.tdoa import (
    MAX_USABLE_SYNC_NS,
    SPEED_OF_LIGHT,
    TdoaError,
    TdoaSolver,
    sync_to_range_sigma,
)

ROOM = {"a0": (0.0, 0.0), "a1": (10.0, 0.0), "a2": (10.0, 8.0), "a3": (0.0, 8.0)}


def _differences(anchors: dict, truth: tuple[float, float], ref: str = "a0") -> dict:
    """Exact range differences for a known transmitter position."""
    p = np.array(truth, dtype=float)
    r_ref = float(np.linalg.norm(p - np.array(anchors[ref])))
    return {
        a: float(np.linalg.norm(p - np.array(xy))) - r_ref
        for a, xy in anchors.items() if a != ref
    }


# ----------------------------------------------------------------------
# the physics that decides whether TDoA is usable at all
# ----------------------------------------------------------------------


def test_one_nanosecond_is_thirty_centimetres():
    """The line that makes TDoA hard."""
    assert sync_to_range_sigma(1.0) == pytest.approx(0.2998, abs=1e-4)
    assert sync_to_range_sigma(0.0) == 0.0
    # 334 ps for a 10 cm target.
    assert sync_to_range_sigma(0.334) == pytest.approx(0.1, abs=0.002)


def test_ntp_class_sync_is_reported_as_unusable():
    """NTP over Wi-Fi is ~1 ms = ~300 km. Not 'less accurate' -- useless."""
    solver = TdoaSolver(ROOM, sync_sigma_ns=1_000_000.0)
    assert not solver.usable
    warning = solver.sync_warning()
    assert warning is not None and "wired backbone" in warning


def test_wired_class_sync_is_accepted():
    solver = TdoaSolver(ROOM, sync_sigma_ns=0.1)
    assert solver.usable
    assert solver.sync_warning() is None


def test_the_usability_threshold_is_where_it_claims_to_be():
    assert TdoaSolver(ROOM, sync_sigma_ns=MAX_USABLE_SYNC_NS).usable
    assert not TdoaSolver(ROOM, sync_sigma_ns=MAX_USABLE_SYNC_NS + 0.1).usable


def test_negative_or_nonfinite_sync_is_refused():
    with pytest.raises(TdoaError):
        TdoaSolver(ROOM, sync_sigma_ns=-1.0)
    with pytest.raises(TdoaError):
        TdoaSolver(ROOM, sync_sigma_ns=float("nan"))
    with pytest.raises(TdoaError):
        sync_to_range_sigma(float("inf"))


# ----------------------------------------------------------------------
# geometry requirements
# ----------------------------------------------------------------------


def test_two_anchors_are_refused_at_construction():
    """Two anchors leave a whole hyperbola, not a point."""
    with pytest.raises(TdoaError, match="at least 3"):
        TdoaSolver({"a0": (0.0, 0.0), "a1": (5.0, 0.0)}, sync_sigma_ns=0.1)


def test_a_single_difference_yields_no_fix():
    solver = TdoaSolver(ROOM, sync_sigma_ns=0.1)
    assert solver.solve({"a1": 1.0}, reference="a0") is None


def test_collinear_anchors_give_no_fix_rather_than_a_wrong_one():
    line = {"a0": (0.0, 0.0), "a1": (5.0, 0.0), "a2": (10.0, 0.0), "a3": (15.0, 0.0)}
    solver = TdoaSolver(line, sync_sigma_ns=0.1)
    fix = solver.solve(_differences(line, (7.0, 6.0)))
    # Either rejected outright, or flagged by a large GDOP -- never a
    # confident fix, because the layout cannot distinguish +y from -y.
    assert fix is None or fix.gdop > 5.0


# ----------------------------------------------------------------------
# the solve itself
# ----------------------------------------------------------------------


@pytest.mark.parametrize("truth", [(3.5, 5.5), (1.0, 1.0), (9.0, 7.0), (5.0, 4.0)])
def test_noise_free_solve_recovers_the_transmitter(truth):
    solver = TdoaSolver(ROOM, sync_sigma_ns=0.0)
    fix = solver.solve(_differences(ROOM, truth))
    assert fix is not None
    assert fix.x == pytest.approx(truth[0], abs=1e-3)
    assert fix.y == pytest.approx(truth[1], abs=1e-3)
    assert fix.residual_m < 1e-6


def test_result_is_independent_of_the_starting_guess():
    solver = TdoaSolver(ROOM, sync_sigma_ns=0.0)
    tdoa = _differences(ROOM, (3.5, 5.5))
    solutions = [solver.solve(tdoa, guess=g)
                 for g in [(5.0, 4.0), (0.0, 0.0), (9.0, 7.0), (2.0, 6.0)]]
    assert all(s is not None for s in solutions)
    for s in solutions:
        assert s.x == pytest.approx(3.5, abs=1e-2)
        assert s.y == pytest.approx(5.5, abs=1e-2)


def test_accuracy_degrades_with_sync_the_way_the_docs_claim():
    """0.1 ns -> centimetres; 1 ns -> tens of centimetres."""
    rng = np.random.default_rng(7)
    truth = (3.5, 5.5)
    exact = _differences(ROOM, truth)

    for sync_ns, ceiling in ((0.1, 0.10), (1.0, 0.60)):
        solver = TdoaSolver(ROOM, sync_sigma_ns=sync_ns, range_sigma_m=0.0)
        noise_m = sync_to_range_sigma(sync_ns)
        errors = []
        for _ in range(200):
            noisy = {a: v + rng.normal(0.0, noise_m) for a, v in exact.items()}
            fix = solver.solve(noisy)
            if fix is not None:
                errors.append(math.dist((fix.x, fix.y), truth))
        assert len(errors) > 150, "too many solves rejected at usable sync"
        assert float(np.mean(errors)) < ceiling


def test_gross_sync_error_produces_no_fix_not_a_wild_one():
    """At 20 ns the hyperbolas stop intersecting.

    Unconstrained Gauss-Newton runs to ~1e17 m here. A fix that large would
    still render as a marker, so it must be refused.
    """
    rng = np.random.default_rng(11)
    exact = _differences(ROOM, (3.5, 5.5))
    solver = TdoaSolver(ROOM, sync_sigma_ns=1000.0, range_sigma_m=0.0)
    noise_m = sync_to_range_sigma(1000.0)

    for _ in range(50):
        noisy = {a: v + rng.normal(0.0, noise_m) for a, v in exact.items()}
        fix = solver.solve(noisy)
        if fix is not None:
            # Anything returned must at least be inside the sanity radius.
            assert math.hypot(fix.x, fix.y) <= 1000.0


def test_reported_sigma_grows_with_sync_error():
    truth = (3.5, 5.5)
    tdoa = _differences(ROOM, truth)
    tight = TdoaSolver(ROOM, sync_sigma_ns=0.1).solve(tdoa)
    loose = TdoaSolver(ROOM, sync_sigma_ns=5.0).solve(tdoa)
    assert tight is not None and loose is not None
    assert loose.sigma_m > tight.sigma_m * 10


def test_sigma_is_not_silently_optimistic():
    """A 1 ns anchor sync cannot yield a centimetre-class sigma."""
    fix = TdoaSolver(ROOM, sync_sigma_ns=1.0).solve(_differences(ROOM, (5.0, 4.0)))
    assert fix is not None
    assert fix.sigma_m > 0.1, "sigma ignores the sync term"


def test_non_finite_measurements_are_rejected():
    solver = TdoaSolver(ROOM, sync_sigma_ns=0.1)
    bad = _differences(ROOM, (3.0, 3.0))
    bad["a1"] = float("nan")
    assert solver.solve(bad) is None


def test_payload_is_serialisable():
    fix = TdoaSolver(ROOM, sync_sigma_ns=0.1).solve(_differences(ROOM, (3.5, 5.5)))
    assert fix is not None
    payload = fix.as_dict()
    assert set(payload) == {
        "x", "y", "sigma_m", "residual_m", "anchors_used", "iterations", "gdop",
    }
    assert payload["anchors_used"] == 4
    assert all(isinstance(v, (int, float)) for v in payload.values())


def test_anchor_positions_must_be_finite():
    with pytest.raises(TdoaError):
        TdoaSolver({"a0": (0.0, 0.0), "a1": (float("nan"), 0.0), "a2": (1.0, 1.0)},
                   sync_sigma_ns=0.1)
