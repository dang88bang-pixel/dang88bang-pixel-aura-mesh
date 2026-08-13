"""Sensor fault detection.

The point of these tests is the *silent* fault: a driver that keeps returning
frames on time while the values have stopped changing. A dead cable already
shows up in `SensorStatus.age_seconds`; a wedged sensor does not.
"""

from __future__ import annotations

import math

import pytest

from aura.sensor_health import (
    HealthState,
    HealthVerdict,
    RateMonitor,
    SensorHealthMonitor,
    StuckDetector,
    inflate_sigma,
)


# ----------------------------------------------------------------------
# stuck detection
# ----------------------------------------------------------------------


def test_changing_payload_is_never_stuck():
    d = StuckDetector(threshold=5)
    for i in range(200):
        assert d.observe({"range": i}) is False
    assert d.repeats == 0


def test_repeated_payload_trips_at_the_threshold():
    d = StuckDetector(threshold=5)
    frame = {"range": 3.21, "anchor": "a0"}
    # First observation sets the baseline; repeats accumulate after it.
    for _ in range(5):
        assert d.observe(frame) is False
    assert d.observe(frame) is True
    assert d.is_stuck


def test_one_changed_frame_clears_the_count():
    """A sensor that recovers must stop being reported as stuck."""
    d = StuckDetector(threshold=3)
    for _ in range(10):
        d.observe({"v": 1})
    assert d.is_stuck
    d.observe({"v": 2})
    assert not d.is_stuck
    assert d.repeats == 0


def test_a_gap_does_not_reset_the_repeat_count():
    """An intermittently wedged sensor must still be caught.

    If None reset the counter, a driver alternating between a dropped frame
    and a repeated one would look healthy forever.
    """
    d = StuckDetector(threshold=4)
    frame = {"v": 7}
    for _ in range(20):
        d.observe(frame)
        d.observe(None)
    assert d.is_stuck


def test_float_payloads_compare_by_value():
    d = StuckDetector(threshold=3)
    for _ in range(10):
        d.observe(1.0)
    assert d.is_stuck
    d.observe(1.0000001)
    assert not d.is_stuck, "a genuinely different value must clear the flag"


def test_stuck_duration_is_reported():
    d = StuckDetector(threshold=2)
    t = 1000.0
    for i in range(10):
        d.observe({"v": 1}, now=t + i * 0.1)
    assert d.is_stuck
    assert d.stuck_seconds(now=t + 1.0) == pytest.approx(0.9, abs=0.05)


def test_unrepresentable_payload_does_not_raise():
    class Hostile:
        def __repr__(self):
            raise RuntimeError("no repr for you")

    d = StuckDetector(threshold=2)
    for _ in range(5):
        d.observe(Hostile())
    # Must not propagate; a broken __repr__ is the driver's problem.
    assert d.is_stuck


# ----------------------------------------------------------------------
# rate monitoring
# ----------------------------------------------------------------------


def test_rate_is_unknown_until_there_is_history():
    r = RateMonitor(expected_hz=10.0)
    assert r.measured_hz is None
    assert r.starved is False, "must not cry starvation before it has data"


def test_measured_rate_matches_the_feed():
    r = RateMonitor(expected_hz=10.0)
    for i in range(50):
        r.observe(now=1000.0 + i * 0.1)
    assert r.measured_hz == pytest.approx(10.0, rel=0.05)
    assert not r.starved


def test_half_rate_counts_as_starved():
    r = RateMonitor(expected_hz=10.0, tolerance=0.5)
    for i in range(50):
        r.observe(now=1000.0 + i * 0.5)   # 2 Hz
    assert r.starved


def test_a_brief_hiccup_is_not_starvation():
    r = RateMonitor(expected_hz=10.0)
    t = 1000.0
    for i in range(40):
        r.observe(now=t + i * 0.1)
    r.observe(now=t + 4.0 + 0.35)   # one late frame
    for i in range(1, 10):
        r.observe(now=t + 4.35 + i * 0.1)
    assert not r.starved


# ----------------------------------------------------------------------
# the monitor
# ----------------------------------------------------------------------


def test_healthy_driver_is_usable():
    m = SensorHealthMonitor()
    for i in range(60):
        v = m.observe("uwb", {"range": 2.0 + i * 0.01},
                      expected_hz=10.0, now=1000.0 + i * 0.1)
    assert v.state is HealthState.OK
    assert v.usable


def test_frozen_driver_is_caught_and_marked_unusable():
    """The case SensorStatus cannot see: frames on time, values constant."""
    m = SensorHealthMonitor(stuck_threshold=20)
    frame = {"range": 3.0, "anchor": "a0"}
    verdict = None
    for i in range(60):
        verdict = m.observe("uwb", frame, expected_hz=10.0, now=1000.0 + i * 0.1)
    assert verdict.state is HealthState.STUCK
    assert not verdict.usable
    assert "unchanged" in verdict.reason
    assert verdict.detail["repeats"] >= 20


def test_missing_frames_escalate_from_degraded_to_stale():
    m = SensorHealthMonitor()
    m.observe("lidar", {"scan": [1, 2, 3]}, now=1000.0)
    mid = m.observe("lidar", None, stale_after=2.0, now=1001.0)
    assert mid.state is HealthState.DEGRADED
    late = m.observe("lidar", None, stale_after=2.0, now=1003.0)
    assert late.state is HealthState.STALE
    assert not late.usable


def test_recovery_returns_to_ok():
    m = SensorHealthMonitor(stuck_threshold=5)
    for i in range(20):
        m.observe("ble", {"rssi": -60}, now=1000.0 + i * 0.1)
    assert m.observe("ble", {"rssi": -60}, now=1002.1).state is HealthState.STUCK
    v = m.observe("ble", {"rssi": -61}, now=1002.2)
    assert v.state is HealthState.OK and v.usable


def test_drivers_are_tracked_independently():
    m = SensorHealthMonitor(stuck_threshold=5)
    for i in range(20):
        m.observe("uwb", {"range": 1.0})            # frozen
        m.observe("lidar", {"scan": i})             # healthy
    assert m.verdicts()["uwb"].state is HealthState.STUCK
    assert m.verdicts()["lidar"].state is HealthState.OK
    assert m.worst() is HealthState.STUCK


def test_worst_is_ok_when_nothing_has_been_seen():
    assert SensorHealthMonitor().worst() is HealthState.OK


def test_reset_clears_one_driver_only():
    m = SensorHealthMonitor(stuck_threshold=3)
    for _ in range(10):
        m.observe("uwb", {"r": 1})
        m.observe("ble", {"r": 1})
    m.reset("uwb")
    assert m.verdicts().get("uwb") is None or not m.verdicts()["uwb"].state is HealthState.STUCK
    assert m.verdicts()["ble"].state is HealthState.STUCK


def test_verdict_serialises():
    m = SensorHealthMonitor(stuck_threshold=2)
    for i in range(6):
        v = m.observe("thermal", {"frame": 1}, now=1000.0 + i * 0.1)
    payload = v.as_dict()
    assert payload["name"] == "thermal"
    assert payload["state"] == "stuck"
    assert payload["usable"] is False


# ----------------------------------------------------------------------
# what to do with a suspect driver
# ----------------------------------------------------------------------


def test_healthy_sigma_is_untouched():
    v = HealthVerdict("uwb", HealthState.OK, "nominal")
    assert inflate_sigma(0.12, v) == 0.12


def test_degraded_sigma_is_inflated_not_dropped():
    v = HealthVerdict("uwb", HealthState.DEGRADED, "slow")
    assert inflate_sigma(0.12, v) == pytest.approx(0.36)


def test_stuck_sigma_is_infinite_so_the_update_must_be_skipped():
    """There is no finite sigma that makes a constant reading informative."""
    for state in (HealthState.STUCK, HealthState.STALE):
        assert math.isinf(inflate_sigma(0.12, HealthVerdict("uwb", state, "x")))


def test_inflate_rejects_a_nonsense_base():
    v = HealthVerdict("uwb", HealthState.OK, "nominal")
    for bad in (0.0, -1.0, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            inflate_sigma(bad, v)


def test_severity_ordering_is_what_worst_relies_on():
    assert HealthState.OK.severity < HealthState.DEGRADED.severity
    assert HealthState.DEGRADED.severity < HealthState.STUCK.severity
    assert HealthState.STUCK.severity < HealthState.STALE.severity


# ----------------------------------------------------------------------
# integration: the fusion loop must not be fooled by a frozen driver
# ----------------------------------------------------------------------


def test_fusion_flags_a_frozen_driver_and_stops_trusting_it():
    """A wedged UWB driver used to shrink sigma while error grew.

    Measured before this guard: 5.4x overconfident after 600 ticks
    (0.46 m error against a 0.086 m sigma).
    """
    import copy
    import time as _time

    from aura.config import AgentConfig
    from aura.fusion import FusionPipeline

    pipe = FusionPipeline(AgentConfig(simulate=True, db_path=":memory:"))
    for d in pipe.drivers:
        d.open()
    for _ in range(200):
        pipe.tick()

    assert pipe.state()["sensors"]["uwb"]["healthy"] is True

    frame = pipe.uwb.read()
    if frame is None:
        pytest.skip("uwb driver produced no sample to freeze")

    def frozen(*_a, **_k):
        # Keep the driver's own bookkeeping healthy: frames arrive on time,
        # only the content is stale. This is what SensorStatus cannot see.
        pipe.uwb.status.samples += 1
        pipe.uwb.status.last_sample_ts = _time.time()
        return copy.deepcopy(frame)

    pipe.uwb.read = frozen
    for _ in range(400):
        pipe.tick()

    uwb = pipe.state()["sensors"]["uwb"]
    assert uwb["health"] == "stuck"
    assert uwb["healthy"] is False, "a frozen driver must not report healthy"
    assert "unchanged" in uwb["health_reason"]

    ekf = pipe.state()["ekf"]
    truth = pipe.imu.ground_truth()
    if truth is not None:
        error = math.dist(ekf["position"][:2], truth[:2])
        sigma = max(ekf["position_sigma"])
        # The filter must not claim more precision than it has.
        assert sigma > error / 2.0, (
            f"sigma {sigma:.3f} too optimistic for error {error:.3f}"
        )
