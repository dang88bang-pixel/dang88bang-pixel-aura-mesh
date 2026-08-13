"""EKF unit tests: kinematics, Jacobians, updates and numerical stability."""

from __future__ import annotations

import math

import numpy as np
import pytest

from aura.ekf import (
    ExtendedKalmanFilter,
    EkfConfig,
    euler_to_matrix,
    matrix_to_quaternion,
    wrap_pi,
    _d_rotation_d_euler,
)

GRAVITY_BODY = (0.0, 0.0, 9.80665)


def test_wrap_pi():
    assert wrap_pi(0.0) == pytest.approx(0.0)
    assert wrap_pi(math.pi) == pytest.approx(math.pi)
    assert wrap_pi(3 * math.pi) == pytest.approx(math.pi)
    assert wrap_pi(-3 * math.pi) == pytest.approx(math.pi)
    assert wrap_pi(2 * math.pi + 0.5) == pytest.approx(0.5)


def test_euler_to_matrix_is_orthonormal():
    for roll, pitch, yaw in [(0.1, -0.2, 0.3), (0.0, 0.0, 0.0), (-1.2, 0.4, 2.9)]:
        R = euler_to_matrix(roll, pitch, yaw)
        assert np.allclose(R @ R.T, np.eye(3), atol=1e-12)
        assert np.linalg.det(R) == pytest.approx(1.0)


def test_quaternion_roundtrip():
    for roll, pitch, yaw in [(0.3, -0.4, 1.1), (0.0, 0.0, math.pi / 2), (-0.9, 0.2, -2.4)]:
        R = euler_to_matrix(roll, pitch, yaw)
        x, y, z, w = matrix_to_quaternion(R)
        assert math.sqrt(x * x + y * y + z * z + w * w) == pytest.approx(1.0, abs=1e-9)
        # rebuild rotation from the quaternion and compare
        Rq = np.array([
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ])
        assert np.allclose(R, Rq, atol=1e-9)


def test_rotation_jacobian_matches_numeric():
    roll, pitch, yaw = 0.21, -0.34, 1.02
    a_body = np.array([0.4, -1.1, 9.6])
    analytic = _d_rotation_d_euler(roll, pitch, yaw, a_body)
    eps = 1e-6
    numeric = np.zeros((3, 3))
    base = np.array([roll, pitch, yaw])
    for i in range(3):
        plus = base.copy()
        minus = base.copy()
        plus[i] += eps
        minus[i] -= eps
        numeric[:, i] = (euler_to_matrix(*plus) @ a_body - euler_to_matrix(*minus) @ a_body) / (2 * eps)
    assert np.allclose(analytic, numeric, atol=1e-5)


def test_stationary_filter_does_not_drift():
    ekf = ExtendedKalmanFilter()
    for _ in range(400):
        ekf.predict((0.0, 0.0, 0.0), GRAVITY_BODY, 0.05)
        ekf.update_zero_velocity()
    assert np.linalg.norm(ekf.position) < 0.05
    assert np.linalg.norm(ekf.velocity) < 0.02


def test_constant_acceleration_integration():
    ekf = ExtendedKalmanFilter()
    dt = 0.01
    accel_x = 1.0
    for _ in range(200):  # 2 seconds
        ekf.predict((0.0, 0.0, 0.0), (accel_x, 0.0, 9.80665), dt)
    # s = 0.5*a*t^2 = 0.5 * 1 * 4 = 2 m
    assert ekf.position[0] == pytest.approx(2.0, rel=0.02)
    assert ekf.velocity[0] == pytest.approx(2.0, rel=0.02)


def test_position_update_pulls_state():
    ekf = ExtendedKalmanFilter()
    for _ in range(30):
        ekf.update_position((5.0, -3.0, 1.4), 0.05)
    assert ekf.position[0] == pytest.approx(5.0, abs=0.05)
    assert ekf.position[1] == pytest.approx(-3.0, abs=0.05)
    assert ekf.position[2] == pytest.approx(1.4, abs=0.05)


def test_uwb_range_trilateration_converges():
    ekf = ExtendedKalmanFilter()
    ekf.x[0], ekf.x[1], ekf.x[2] = 1.0, 1.0, 1.4
    truth = np.array([6.0, 4.0, 1.4])
    anchors = [(0.0, 0.0, 2.4), (12.0, 0.0, 2.4), (12.0, 9.0, 2.4), (0.0, 9.0, 2.4)]
    for _ in range(120):
        for anchor in anchors:
            distance = float(np.linalg.norm(truth - np.asarray(anchor)))
            ekf.update_uwb_range(anchor, distance, sigma=0.05)
        ekf.update_altitude(1.4, sigma=0.1)
    assert np.linalg.norm(ekf.position[:2] - truth[:2]) < 0.15


def test_yaw_update_handles_wraparound():
    ekf = ExtendedKalmanFilter()
    ekf.x[8] = 3.0
    for _ in range(40):
        ekf.update_yaw(-3.0, sigma=0.05)
    # -3.0 is only 0.28 rad away across the +/-pi seam, not 6 rad
    assert abs(wrap_pi(ekf.x[8] - (-3.0))) < 0.1


def test_lidar_update_leaves_altitude_alone():
    ekf = ExtendedKalmanFilter()
    ekf.x[2] = 1.4
    z_variance_before = ekf.P[2, 2]
    for _ in range(20):
        ekf.update_lidar_pose((2.0, 3.0), 0.5)
    assert ekf.x[2] == pytest.approx(1.4, abs=1e-6)
    assert ekf.P[2, 2] >= z_variance_before - 1e-12


def test_covariance_stays_symmetric_positive_definite():
    ekf = ExtendedKalmanFilter()
    rng = np.random.default_rng(3)
    for _ in range(300):
        ekf.predict(rng.normal(0, 0.05, 3), (0.0, 0.0, 9.80665) + rng.normal(0, 0.1, 3), 0.05)
        ekf.update_position(rng.normal(0, 1, 3), 0.5)
        ekf.update_uwb_range((0.0, 0.0, 2.0), float(abs(rng.normal(5, 0.2))))
    assert np.allclose(ekf.P, ekf.P.T, atol=1e-10)
    eigenvalues = np.linalg.eigvalsh(ekf.P)
    assert eigenvalues.min() > 0.0
    assert np.all(np.isfinite(ekf.P))


def test_snapshot_and_transform_serialise():
    ekf = ExtendedKalmanFilter()
    ekf.predict((0.01, 0.0, 0.02), GRAVITY_BODY, 0.05)
    snap = ekf.snapshot(123.0).as_dict()
    assert snap["timestamp"] == 123.0
    assert len(snap["position"]) == 3
    assert len(snap["covariance_diagonal"]) == 15
    assert len(snap["orientation_quaternion"]) == 4
    transform = ekf.transform()
    assert len(transform["matrix"]) == 16
    assert set(transform) >= {"offset_x", "offset_y", "offset_z", "roll", "pitch", "yaw"}


def test_dt_is_clamped():
    ekf = ExtendedKalmanFilter(EkfConfig(max_dt=0.2))
    ekf.predict((0.0, 0.0, 0.0), (5.0, 0.0, 9.80665), 100.0)  # a scheduler stall
    assert np.all(np.abs(ekf.position) < 1.0)
    assert np.all(np.isfinite(ekf.x))
