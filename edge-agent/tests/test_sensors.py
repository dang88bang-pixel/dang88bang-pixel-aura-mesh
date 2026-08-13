"""Driver tests: parsers, simulators and the world model they ray-cast against."""

from __future__ import annotations

import math
import struct
import time

import numpy as np
import pytest

from aura.doppler import MicroDopplerAnalyzer, ThroughWallTracker, VitalSigns, band_peak, detrend
from aura.sensors import BleDriver, ImuDriver, LidarDriver, MmwaveDriver, ThermalDriver, UwbDriver
from aura.sensors.imu import StaticDetector
from aura.sensors.mmwave import MAGIC_WORD
from aura.world import WORLD, World, Wall


# ----------------------------------------------------------------------
# world
# ----------------------------------------------------------------------
def test_raycast_hits_known_wall():
    world = World([Wall(5.0, -5.0, 5.0, 5.0)])
    assert world.raycast((0.0, 0.0), 0.0, 20.0) == pytest.approx(5.0, abs=1e-6)
    assert world.raycast((0.0, 0.0), math.pi, 20.0) == pytest.approx(20.0)


def test_visibility_and_wall_counting():
    world = World([Wall(5.0, -5.0, 5.0, 5.0)])
    assert not world.visible((0.0, 0.0), (10.0, 0.0))
    assert world.visible((0.0, 0.0), (4.0, 0.0))
    assert world.wall_count_between((0.0, 0.0), (10.0, 0.0)) == 1
    assert world.wall_count_between((0.0, 0.0), (4.0, 0.0)) == 0


def test_default_floorplan_is_mostly_enclosed():
    # A few rays legitimately escape through the doorways and the two exits,
    # but the overwhelming majority must terminate on a wall.
    scan = WORLD.scan((10.0, 6.5), 0.0, beams=360, max_range=40.0)
    escaped = np.count_nonzero(scan[:, 1] >= 40.0)
    assert escaped / len(scan) < 0.1
    assert float(np.median(scan[:, 1])) < 12.0


def test_is_free_rejects_wall_points():
    assert WORLD.is_free(10.0, 6.5)
    assert not WORLD.is_free(0.0, 7.0)  # on the outer shell


# ----------------------------------------------------------------------
# lidar
# ----------------------------------------------------------------------
def test_lidar_simulator_matches_geometry():
    lidar = LidarDriver(simulate=True, world=WORLD, beams=180)
    lidar.open()
    lidar.set_pose(10.0, 6.5, 0.0)
    scan = lidar.read()
    assert scan is not None and len(scan) > 100
    assert all(0.0 < d <= lidar.max_range for d in scan.distances)
    points = scan.to_cartesian((10.0, 6.5, 0.0))
    assert points.shape == (len(scan), 2)
    # projected points must land near the true geometry
    errors = [abs(math.hypot(px - 10.0, py - 6.5) - d) for (px, py), d in zip(points, scan.distances)]
    assert max(errors) < 1e-6


def test_lidar_packet_parser_decodes_legacy_nodes():
    lidar = LidarDriver(port="/dev/null", simulate=False)
    lidar.simulate = False
    lidar._scan_started = True

    def make_node(angle_deg: float, distance_m: float, quality: int = 40) -> bytes:
        angle_q6 = int(angle_deg * 64) & 0x7FFF
        dist_q2 = int(distance_m * 4000)
        b0 = (quality << 2) | 0x01           # S=1, !S=0
        b1 = ((angle_q6 & 0x7F) << 1) | 0x01  # check bit
        b2 = (angle_q6 >> 7) & 0xFF
        return bytes([b0, b1, b2]) + struct.pack("<H", dist_q2)

    lidar._buffer.extend(make_node(90.0, 2.5) + make_node(180.0, 4.0))
    scan = lidar._read_hardware()
    assert scan is not None
    assert len(scan) == 2
    assert scan.distances[0] == pytest.approx(2.5, abs=0.01)
    assert scan.distances[1] == pytest.approx(4.0, abs=0.01)
    assert scan.angles[0] == pytest.approx(math.radians(90.0) - math.pi, abs=0.01)


# ----------------------------------------------------------------------
# mmwave
# ----------------------------------------------------------------------
def test_mmwave_tlv_parser():
    driver = MmwaveDriver(port="/dev/null", simulate=False)
    driver.simulate = False
    points = [(1.0, 2.0, 0.1, 0.5), (3.0, -1.0, 0.0, -1.2)]
    payload = b"".join(struct.pack("<4f", *p) for p in points)
    tlv = struct.pack("<2I", 1, 8 + len(payload)) + payload
    header = struct.pack("<8I", 1, 40 + len(tlv), 0, 0, 0, 0, 1, 0)
    frame = MAGIC_WORD + header[4:] + b"\x00" * 4 + tlv
    # rebuild precisely: magic(8) + 32 bytes header fields + tlv
    header_fields = struct.pack("<8I", 1, 0, 40 + len(tlv), 0, 0, 0, 1, len(points))
    frame = MAGIC_WORD + header_fields + tlv
    driver._buffer.extend(frame)
    targets = driver._read_hardware()
    assert len(targets) == 2
    assert targets[0].x == pytest.approx(1.0)
    assert targets[1].velocity == pytest.approx(-1.2)


def test_mmwave_simulator_sees_people_and_clutter():
    driver = MmwaveDriver(simulate=True, world=WORLD)
    driver.open()
    driver.set_pose(10.0, 4.0, math.pi / 2)          # looking towards +y
    driver.set_people([np.array([10.2, 7.0, 1.0])])  # 3 m straight ahead
    targets = driver.read()
    assert targets
    moving = [t for t in targets if t.track_id >= 0]
    assert moving, "a walking person must produce a Doppler target"
    assert moving[0].range_m == pytest.approx(3.0, abs=0.6)


def test_mmwave_profile_switch():
    driver = MmwaveDriver(simulate=True)
    driver.open()
    driver.configure_profile(reduced=True)
    assert driver.reduced
    assert driver.info["profile"] == "reduced"


# ----------------------------------------------------------------------
# uwb
# ----------------------------------------------------------------------
def test_uwb_ranges_are_plausible():
    driver = UwbDriver(simulate=True, world=WORLD)
    driver.open()
    driver.set_pose(10.0, 6.5, 0.0)
    reading = driver.read()
    assert reading is not None
    assert set(reading.ranges) == set(driver.anchors)
    for name, distance in reading.ranges.items():
        ax, ay, az = driver.anchors[name]
        truth = math.dist((10.0, 6.5, 1.4), (ax, ay, az))
        assert abs(distance - truth) < 1.2


def test_uwb_multilateration_recovers_position():
    driver = UwbDriver(simulate=True, world=WORLD)
    driver.open()
    driver.set_pose(7.0, 5.0, 0.0)
    fixes = []
    for _ in range(20):
        reading = driver.read()
        fix = driver.multilaterate(reading.ranges)
        if fix:
            fixes.append(fix)
    assert fixes
    mean = np.mean(fixes, axis=0)
    assert math.hypot(mean[0] - 7.0, mean[1] - 5.0) < 0.6


def test_uwb_hardware_line_parser():
    driver = UwbDriver(port="/dev/null", simulate=False)
    driver.simulate = False
    driver._buffer.extend(b"ANCHOR-A=3.214,ANCHOR-B=7.882;CIR=0.42,1.87\n")
    reading = driver._read_hardware()
    assert reading is not None
    assert reading.ranges["ANCHOR-A"] == pytest.approx(3.214)
    assert reading.cir_amplitude == pytest.approx(0.42)
    assert reading.cir_phase == pytest.approx(1.87)


# ----------------------------------------------------------------------
# ble
# ----------------------------------------------------------------------
def test_ble_rssi_decreases_with_distance():
    driver = BleDriver(simulate=True, world=WORLD)
    driver.open()
    driver.set_pose(10.0, 6.5, 0.0)
    near = {b.address: b.rssi for b in [x for _ in range(15) for x in driver.read()]}
    driver._smoothed.clear()
    driver.set_pose(1.0, 1.0, 0.0)
    far = {b.address: b.rssi for b in [x for _ in range(15) for x in driver.read()]}
    # the corridor token is close to (10, 6.5) and far from (1, 1)
    assert near["AA:BB:CC:00:01"] > far["AA:BB:CC:00:01"]


def test_ble_multilateration_and_token_management():
    driver = BleDriver(simulate=True, world=WORLD)
    driver.open()
    driver.set_pose(8.0, 6.0, 0.0)
    for _ in range(25):
        beacons = driver.read()
    fix = driver.multilaterate(beacons)
    assert fix is not None
    assert len(fix) == 3
    driver.add_token("AA:BB:CC:00:99", "Extra", 5.0, 5.0)
    assert "AA:BB:CC:00:99" in driver.tokens
    assert driver.remove_token("AA:BB:CC:00:99")
    assert not driver.remove_token("does-not-exist")


def test_ble_distance_model():
    from aura.sensors.ble import BleBeacon

    assert BleBeacon("x", -59).distance == pytest.approx(1.0, abs=0.01)
    assert BleBeacon("x", -83).distance > BleBeacon("x", -70).distance


# ----------------------------------------------------------------------
# imu
# ----------------------------------------------------------------------
def test_imu_simulator_produces_gravity():
    imu = ImuDriver(simulate=True, world=WORLD)
    imu.open()
    samples = [imu.read() for _ in range(30)]
    magnitudes = [math.sqrt(sum(v * v for v in s.accel)) for s in samples]
    assert 8.5 < float(np.mean(magnitudes)) < 11.5


def test_imu_hardware_csv_parser():
    imu = ImuDriver(port="/dev/null", simulate=False)
    imu.simulate = False
    imu._buffer.extend(b"0.01,0.02,0.03,0.1,0.2,9.8,10,20,30,35.5\n")
    sample = imu._read_hardware()
    assert sample is not None
    assert sample.gyro == (0.01, 0.02, 0.03)
    assert sample.accel[2] == pytest.approx(9.8)
    assert sample.temperature == pytest.approx(35.5)


def test_static_detector_rejects_walking():
    detector = StaticDetector()
    from aura.sensors.imu import ImuSample

    still = ImuSample(0.0, (0.001, 0.0, 0.0), (0.0, 0.0, 9.80665), (48.0, 0.0, -12.0))
    for _ in range(20):
        result = detector.push(still)
    assert result is True

    detector.reset()
    rng = np.random.default_rng(0)
    for i in range(20):
        bounce = 0.6 * math.sin(2 * math.pi * 1.9 * i * 0.05)
        walking = ImuSample(
            i * 0.05,
            (0.2 * math.cos(i * 0.4), 0.15, 0.05),
            (0.3, 0.2, 9.80665 + bounce + float(rng.normal(0, 0.1))),
            (48.0, 0.0, -12.0),
        )
        result = detector.push(walking)
    assert result is False, "the stance phase of a stride must not trigger a ZUPT"


# ----------------------------------------------------------------------
# thermal
# ----------------------------------------------------------------------
def test_thermal_detects_a_person():
    thermal = ThermalDriver(simulate=True, world=WORLD)
    thermal.open()
    thermal.set_pose(10.0, 4.0, math.pi / 2)
    thermal.set_people([np.array([10.0, 6.5, 0.0])])
    frame = thermal.read()
    assert frame is not None
    assert frame.max_temp > 25.0
    assert frame.hotspots(threshold=25.0)


def test_thermal_empty_scene_has_no_hotspots():
    thermal = ThermalDriver(simulate=True, world=WORLD)
    thermal.open()
    thermal.set_people([])
    frame = thermal.read()
    assert frame.hotspots(threshold=30.0) == []


# ----------------------------------------------------------------------
# doppler
# ----------------------------------------------------------------------
def test_detrend_removes_linear_ramp():
    x = np.linspace(0, 10, 100) * 3.0 + 5.0
    assert abs(float(np.mean(detrend(x)))) < 1e-9
    assert float(np.max(np.abs(detrend(x)))) < 1e-9


def test_band_peak_finds_tone():
    fs = 20.0
    n = 512
    t = np.arange(n) / fs
    signal = np.sin(2 * np.pi * 0.3 * t)
    power = np.abs(np.fft.rfft(signal)) ** 2
    freqs = np.fft.rfftfreq(n, 1 / fs)
    peak, snr = band_peak(freqs, power, (0.1, 0.6))
    assert peak == pytest.approx(0.3, abs=0.02)
    assert snr > 10


def test_micro_doppler_extracts_respiration_and_heartbeat():
    analyzer = MicroDopplerAnalyzer()
    fs = 20.0
    rng = np.random.default_rng(5)
    for i in range(256):
        t = i / fs
        value = (
            0.006 * math.sin(2 * math.pi * 0.28 * t)
            + 0.0007 * math.sin(2 * math.pi * 1.15 * t)
            + float(rng.normal(0, 0.0004))
        )
        analyzer.push(t, value)
    # presence requires a few consecutive windows above the gate
    for _ in range(3):
        vitals = analyzer.analyze()
    assert vitals.presence
    assert vitals.respiration_hz == pytest.approx(0.28, abs=0.03)
    assert vitals.respiration_bpm == pytest.approx(16.8, abs=2.0)
    assert vitals.heart_hz == pytest.approx(1.15, abs=0.12)


def test_micro_doppler_reports_no_presence_on_noise():
    analyzer = MicroDopplerAnalyzer()
    rng = np.random.default_rng(9)
    for i in range(256):
        analyzer.push(i / 20.0, float(rng.normal(0, 0.001)))
    vitals = analyzer.analyze()
    assert vitals.respiration_snr < 12.0


def test_micro_doppler_needs_enough_samples():
    analyzer = MicroDopplerAnalyzer()
    for i in range(10):
        analyzer.push(i / 20.0, 0.1)
    assert analyzer.analyze().presence is False


def test_through_wall_tracker_stabilises_ids():
    tracker = ThroughWallTracker()
    vitals = VitalSigns(respiration_bpm=17.0, presence=True, confidence=0.8)
    now = time.time()
    tracker.update([(3.0, 4.0, True)], vitals, now)
    detections = tracker.update([(3.05, 4.02, True)], vitals, now + 0.1)
    assert len(detections) == 1
    first_id = detections[0].detection_id
    detections = tracker.update([(3.1, 4.05, True)], vitals, now + 0.2)
    assert detections[0].detection_id == first_id
    assert detections[0].behind_wall
    # a stale track must expire
    assert tracker.update([], vitals, now + 10.0) == []


def test_micro_doppler_false_positive_rate_on_noise():
    """The presence gate must not fire on an empty room.

    Regression test: the original 6 dB threshold produced a 33 % false-positive
    rate on pure noise, i.e. the system reported people breathing behind walls
    in an empty building. Noise peaks at ~9 dB, real breathing at 33+ dB.
    """
    false_positives = 0
    trials = 60
    for trial in range(trials):
        rng = np.random.default_rng(trial)
        analyzer = MicroDopplerAnalyzer()
        for i in range(256):
            analyzer.push(i / 20.0, float(rng.normal(0, 0.001)))
        vitals = None
        for _ in range(4):          # persistence needs several windows
            vitals = analyzer.analyze()
        if vitals.presence:
            false_positives += 1
    assert false_positives == 0, f"{false_positives}/{trials} false detections in an empty room"


def test_micro_doppler_still_detects_a_real_subject():
    """The stricter gate must not cost us true detections."""
    detections = 0
    trials = 25
    for trial in range(trials):
        rng = np.random.default_rng(1000 + trial)
        analyzer = MicroDopplerAnalyzer()
        for i in range(256):
            t = i / 20.0
            analyzer.push(t, 0.006 * math.sin(2 * math.pi * 0.28 * t) + float(rng.normal(0, 0.0008)))
        vitals = None
        for _ in range(4):
            vitals = analyzer.analyze()
        if vitals.presence and abs(vitals.respiration_bpm - 16.8) < 3.0:
            detections += 1
    assert detections == trials, f"only {detections}/{trials} true subjects detected"


def test_presence_requires_temporal_persistence():
    """One good window is not enough; noise spikes must be rejected."""
    analyzer = MicroDopplerAnalyzer(presence_hits=3)
    for i in range(256):
        t = i / 20.0
        analyzer.push(t, 0.006 * math.sin(2 * math.pi * 0.28 * t))
    assert analyzer.analyze().presence is False, "first window must not assert presence"
    analyzer.analyze()
    assert analyzer.analyze().presence is True, "presence after the third window"
