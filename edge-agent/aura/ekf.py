"""6-DOF Extended Kalman Filter for the Aura sensor-fusion stack.

The filter estimates a 15-element state vector::

    x = [ px, py, pz,            # position in the map frame  [m]
          vx, vy, vz,            # velocity in the map frame  [m/s]
          roll, pitch, yaw,      # orientation (ZYX Euler)    [rad]
          bgx, bgy, bgz,         # gyroscope bias             [rad/s]
          bax, bay, baz ]        # accelerometer bias         [m/s^2]

The implementation deliberately avoids SciPy and any exotic NumPy feature so
that it can be ported 1:1 to Kotlin (see
``android-app/app/src/main/java/com/example/agent/fusion/EkfFusion.kt``).
Every matrix operation used here has a direct counterpart in the Kotlin
``Mat`` helper class.

Conventions
-----------
* Right-handed map frame, ``z`` up.
* Body frame: ``x`` forward, ``y`` left, ``z`` up.
* Rotation matrix ``R = Rz(yaw) @ Ry(pitch) @ Rx(roll)`` maps body -> map.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Sequence

import numpy as np

STATE_DIM = 15

IDX_POS = slice(0, 3)
IDX_VEL = slice(3, 6)
IDX_ATT = slice(6, 9)
IDX_BG = slice(9, 12)
IDX_BA = slice(12, 15)

GRAVITY = np.array([0.0, 0.0, -9.80665])


def wrap_pi(angle: float) -> float:
    """Wrap an angle to (-pi, pi]."""
    wrapped = math.fmod(angle + math.pi, 2.0 * math.pi)
    if wrapped <= 0.0:
        wrapped += 2.0 * math.pi
    return wrapped - math.pi


def euler_to_matrix(roll: float, pitch: float, yaw: float) -> np.ndarray:
    """ZYX Euler angles -> body-to-map rotation matrix."""
    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    return np.array(
        [
            [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
            [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
            [-sp, cp * sr, cp * cr],
        ]
    )


def matrix_to_quaternion(r: np.ndarray) -> tuple[float, float, float, float]:
    """Rotation matrix -> quaternion (x, y, z, w), Three.js ordering."""
    trace = r[0, 0] + r[1, 1] + r[2, 2]
    if trace > 0.0:
        s = 0.5 / math.sqrt(trace + 1.0)
        w = 0.25 / s
        x = (r[2, 1] - r[1, 2]) * s
        y = (r[0, 2] - r[2, 0]) * s
        z = (r[1, 0] - r[0, 1]) * s
    elif r[0, 0] > r[1, 1] and r[0, 0] > r[2, 2]:
        s = 2.0 * math.sqrt(max(1e-12, 1.0 + r[0, 0] - r[1, 1] - r[2, 2]))
        w = (r[2, 1] - r[1, 2]) / s
        x = 0.25 * s
        y = (r[0, 1] + r[1, 0]) / s
        z = (r[0, 2] + r[2, 0]) / s
    elif r[1, 1] > r[2, 2]:
        s = 2.0 * math.sqrt(max(1e-12, 1.0 + r[1, 1] - r[0, 0] - r[2, 2]))
        w = (r[0, 2] - r[2, 0]) / s
        x = (r[0, 1] + r[1, 0]) / s
        y = 0.25 * s
        z = (r[1, 2] + r[2, 1]) / s
    else:
        s = 2.0 * math.sqrt(max(1e-12, 1.0 + r[2, 2] - r[0, 0] - r[1, 1]))
        w = (r[1, 0] - r[0, 1]) / s
        x = (r[0, 2] + r[2, 0]) / s
        y = (r[1, 2] + r[2, 1]) / s
        z = 0.25 * s
    return (float(x), float(y), float(z), float(w))


@dataclass
class EkfConfig:
    """Process/measurement noise configuration (all values are 1-sigma)."""

    sigma_accel: float = 0.35          # m/s^2, IMU accelerometer noise
    sigma_gyro: float = 0.02           # rad/s, IMU gyro noise
    sigma_gyro_bias: float = 1.5e-4    # rad/s/sqrt(s), bias random walk
    sigma_accel_bias: float = 8.0e-4   # m/s^2/sqrt(s)
    sigma_lidar_pos: float = 0.06      # m, scan-match position fix
    sigma_uwb_range: float = 0.12      # m, UWB two-way ranging
    sigma_ble_pos: float = 1.40        # m, RSSI multilateration
    sigma_yaw: float = 0.10            # rad, magnetometer heading
    sigma_zupt: float = 0.02           # m/s, zero-velocity update
    sigma_baro_z: float = 0.35         # m, barometric altitude
    max_dt: float = 0.5                # s, clamp for long scheduler stalls
    # Chi-square gate on the normalised innovation. 16.0 is ~4 sigma for a
    # 1-DoF measurement: loose enough that honest noise and a genuine fast
    # movement pass, tight enough to stop a multipath reflection.
    gate_threshold: float = 16.0
    # After this many consecutive rejections the filter distrusts *itself*
    # rather than the sensor, and lets one measurement through to recover.
    max_consecutive_rejects: int = 5
    # Noise inflation applied to that recovery update, so it nudges rather
    # than yanks.
    reject_recovery_inflation: float = 100.0


def mahalanobis_gate(innovation: Iterable[float], covariance: np.ndarray, threshold: float = 16.0) -> bool:
    """Chi-square gate: is this measurement consistent with the estimate?

    `covariance` must be the **innovation** covariance S = H P H' + R, not the
    state covariance P. Returns True to accept.

    A singular S means the filter cannot say how surprising the measurement is,
    so we accept rather than silently starve it of updates. A non-finite
    distance is a different matter and is rejected.
    """
    y = np.asarray(list(innovation), dtype=float)
    if not np.all(np.isfinite(y)) or not np.all(np.isfinite(covariance)):
        return False
    try:
        d2 = float(y @ np.linalg.inv(covariance) @ y)
    except np.linalg.LinAlgError:
        return True
    if not math.isfinite(d2):
        return False
    return d2 <= threshold


# Position-quality tiers, in metres of worst-axis 1-sigma.
#
# Grounded in the standards the system is meant to serve rather than picked to
# look good: NIST PSCR asks for better than 3 m 3D at 95% without beacons, and
# FCC 47 CFR 9.10 requires +/-3 m z-axis for 80% of E911 calls. A 3 m 95% 2D
# requirement is sigma <= 1.23 m, so GOOD at 0.75 m sits comfortably inside it
# while DEGRADED still meets the envelope at about 1 sigma.
#
# A boolean cannot express this. Measured drift after 60 s with no aiding is
# 162 m standing and 4777 m walking; both report "not converged", exactly like
# a 0.8 m estimate that is fine. See docs/open_issues_research.md.
QUALITY_GOOD_M = 0.75
QUALITY_DEGRADED_M = 3.0
QUALITY_POOR_M = 10.0


def classify_quality(sigma_max: float) -> str:
    """Map worst-axis position sigma onto an operator-facing tier."""
    if not math.isfinite(sigma_max):
        return "lost"
    if sigma_max <= QUALITY_GOOD_M:
        return "good"
    if sigma_max <= QUALITY_DEGRADED_M:
        return "degraded"
    if sigma_max <= QUALITY_POOR_M:
        return "poor"
    return "lost"


@dataclass
class EkfSnapshot:
    """Serialisable view of the filter state."""

    timestamp: float
    position: list[float]
    velocity: list[float]
    orientation_euler: list[float]
    orientation_quaternion: list[float]
    gyro_bias: list[float]
    accel_bias: list[float]
    covariance_diagonal: list[float]
    position_sigma: list[float]
    trace: float
    innovation_rms: float
    updates: int
    rejected: int
    converged: bool
    quality: str
    seconds_since_aiding: float

    def as_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "position": self.position,
            "velocity": self.velocity,
            "orientation_euler": self.orientation_euler,
            "orientation_quaternion": self.orientation_quaternion,
            "gyro_bias": self.gyro_bias,
            "accel_bias": self.accel_bias,
            "covariance_diagonal": self.covariance_diagonal,
            "position_sigma": self.position_sigma,
            "trace": self.trace,
            "innovation_rms": self.innovation_rms,
            "updates": self.updates,
            "rejected": self.rejected,
            "converged": self.converged,
            "quality": self.quality,
            "seconds_since_aiding": self.seconds_since_aiding,
        }


class ExtendedKalmanFilter:
    """15-state error-free (direct) EKF with strap-down IMU propagation."""

    def __init__(self, config: EkfConfig | None = None) -> None:
        self.config = config or EkfConfig()
        self.x = np.zeros(STATE_DIM)
        self.P = np.eye(STATE_DIM)
        self.P[IDX_POS, IDX_POS] *= 4.0
        self.P[IDX_VEL, IDX_VEL] *= 1.0
        self.P[IDX_ATT, IDX_ATT] *= 0.25
        self.P[IDX_BG, IDX_BG] *= 1e-3
        self.P[IDX_BA, IDX_BA] *= 1e-2
        self.last_timestamp: float | None = None
        self.updates = 0
        self.rejected = 0
        # Rejection streaks are counted per measurement source: two anchors can
        # be permanently gated out while two others keep being accepted, which
        # a single global counter would never reveal.
        self._reject_streaks: dict[str, int] = {}
        # Timestamp of the last update that actually bounds *position*.
        # Deliberately not set by update_yaw or update_zero_velocity: those
        # constrain heading and velocity, but neither stops position drifting,
        # which is the thing the operator needs warned about.
        self.last_aiding_time: float | None = None
        self._clock = 0.0
        self._innovations: list[float] = []

    # ------------------------------------------------------------------
    # accessors
    # ------------------------------------------------------------------
    @property
    def position(self) -> np.ndarray:
        return self.x[IDX_POS].copy()

    @property
    def velocity(self) -> np.ndarray:
        return self.x[IDX_VEL].copy()

    @property
    def attitude(self) -> np.ndarray:
        return self.x[IDX_ATT].copy()

    @property
    def rotation_matrix(self) -> np.ndarray:
        return euler_to_matrix(*self.x[IDX_ATT])

    def reset(self, position: Sequence[float] | None = None, yaw: float = 0.0) -> None:
        self.__init__(self.config)  # type: ignore[misc]
        if position is not None:
            self.x[IDX_POS] = np.asarray(position, dtype=float)
        self.x[8] = wrap_pi(yaw)

    # ------------------------------------------------------------------
    # prediction
    # ------------------------------------------------------------------
    def _note_aiding(self) -> None:
        """Record that position was constrained by a real measurement."""
        self.last_aiding_time = self._clock

    def predict(self, gyro: Sequence[float], accel: Sequence[float], dt: float) -> None:
        """Strap-down propagation with an IMU sample.

        ``gyro`` is body angular rate [rad/s], ``accel`` is specific force in
        the body frame [m/s^2] (i.e. what an accelerometer reports).
        """
        self._clock += dt
        dt = float(min(max(dt, 1e-4), self.config.max_dt))
        cfg = self.config
        w = np.asarray(gyro, dtype=float) - self.x[IDX_BG]
        a_body = np.asarray(accel, dtype=float) - self.x[IDX_BA]

        roll, pitch, yaw = self.x[IDX_ATT]
        R = euler_to_matrix(roll, pitch, yaw)
        a_map = R @ a_body + GRAVITY

        # --- nominal state propagation (Euler integration) ---
        self.x[IDX_POS] = self.x[IDX_POS] + self.x[IDX_VEL] * dt + 0.5 * a_map * dt * dt
        self.x[IDX_VEL] = self.x[IDX_VEL] + a_map * dt

        # Euler-rate kinematics: d(euler)/dt = T(euler) * omega_body
        cp = math.cos(pitch)
        if abs(cp) < 1e-4:
            cp = math.copysign(1e-4, cp if cp != 0.0 else 1.0)
        tp = math.tan(pitch)
        sr, cr = math.sin(roll), math.cos(roll)
        T = np.array(
            [
                [1.0, sr * tp, cr * tp],
                [0.0, cr, -sr],
                [0.0, sr / cp, cr / cp],
            ]
        )
        euler_dot = T @ w
        self.x[IDX_ATT] = np.array([wrap_pi(v) for v in (self.x[IDX_ATT] + euler_dot * dt)])

        # --- Jacobian F = d f / d x ---
        F = np.eye(STATE_DIM)
        F[IDX_POS, IDX_VEL] = np.eye(3) * dt
        F[IDX_VEL, IDX_ATT] = _d_rotation_d_euler(roll, pitch, yaw, a_body) * dt
        F[IDX_VEL, IDX_BA] = -R * dt
        F[IDX_ATT, IDX_BG] = -T * dt

        # --- process noise ---
        Q = np.zeros((STATE_DIM, STATE_DIM))
        qa = cfg.sigma_accel ** 2
        qg = cfg.sigma_gyro ** 2
        Q[IDX_POS, IDX_POS] = np.eye(3) * (0.25 * qa * dt ** 4)
        Q[IDX_VEL, IDX_VEL] = np.eye(3) * (qa * dt ** 2)
        Q[IDX_POS, IDX_VEL] = np.eye(3) * (0.5 * qa * dt ** 3)
        Q[IDX_VEL, IDX_POS] = np.eye(3) * (0.5 * qa * dt ** 3)
        Q[IDX_ATT, IDX_ATT] = np.eye(3) * (qg * dt ** 2)
        Q[IDX_BG, IDX_BG] = np.eye(3) * (cfg.sigma_gyro_bias ** 2 * dt)
        Q[IDX_BA, IDX_BA] = np.eye(3) * (cfg.sigma_accel_bias ** 2 * dt)

        self.P = F @ self.P @ F.T + Q
        self._symmetrise()

    # ------------------------------------------------------------------
    # generic update
    # ------------------------------------------------------------------
    def _update(self, z: np.ndarray, h: np.ndarray, H: np.ndarray, R: np.ndarray,
                source: str = "") -> float:
        """Joseph-form measurement update. Returns the innovation norm.

Every sensor update funnels through here, so this is where the two
        things that can wreck the filter get stopped.

        **Non-finite rejection.** A NaN or inf measurement poisons the whole
        state vector in one step -- ``x`` and ``P`` go NaN and never recover,
        and the quality label reads ``lost`` forever. A single corrupted serial
        line can do it: the UWB parser used a bare ``float()``, which happily
        returns ``inf`` for ``"1e400"`` and ``nan`` for ``"nan"``.

        This check is *belt and braces*: ``mahalanobis_gate`` also refuses
        non-finite input, independently of the threshold, so deleting the
        explicit check below leaves every test green. It is kept because it
        states the invariant at the top of the function where a reader will
        look for it, and because it does not depend on the gate staying the way
        it is today. It is not, however, load-bearing on its own -- see
        ``test_non_finite_guard_holds_even_with_the_chi_square_gate_disabled``.

        **Chi-square gate.** A measurement wildly inconsistent with the current
        estimate is far more likely a multipath reflection or a parse error
        than a real jump. Measured on this pipeline before the gate: one
        +200 m range moved the estimate 8 m *permanently* while sigma stayed at
        0.09 and quality stayed ``good``.
        """
        if not (np.all(np.isfinite(z)) and np.all(np.isfinite(h))
                and np.all(np.isfinite(H)) and np.all(np.isfinite(R))):
            self.rejected += 1
            return 0.0

        y = z - h
        # angles (if any) must be wrapped by the caller before this point
        S = H @ self.P @ H.T + R
        try:
            K = self.P @ H.T @ np.linalg.inv(S)
        except np.linalg.LinAlgError:  # pragma: no cover - numerically degenerate
            return 0.0

        # Gate against the *innovation covariance* S, not P: S is what says how
        # surprising this measurement should be, given both the state
        # uncertainty and the sensor's own noise.
        #
        # The consecutive-rejection escape hatch is not optional. A filter that
        # has become overconfident -- P collapsed far below the true error --
        # finds every honest measurement "surprising" and gates it away, which
        # keeps P small, which rejects the next one. Measured without the
        # escape: a cold-start trilateration rejected 359 updates (two anchors
        # locked out permanently) and
        # settled 0.36 m off, converging to the wrong answer with high
        # confidence. Persistent rejection means the *estimate* is wrong, not
        # the sensor, so after a few in a row we let one through to pull the
        # filter back and reset the count.
        if not mahalanobis_gate(y, S, threshold=self.config.gate_threshold):
            streak = self._reject_streaks.get(source, 0) + 1
            self._reject_streaks[source] = streak
            if streak <= self.config.max_consecutive_rejects:
                self.rejected += 1
                return 0.0
            # Fall through: accept this one to recover, but widen R first so a
            # genuine outlier cannot yank the state hard.
            R = R * self.config.reject_recovery_inflation
            S = H @ self.P @ H.T + R
            try:
                K = self.P @ H.T @ np.linalg.inv(S)
            except np.linalg.LinAlgError:  # pragma: no cover
                return 0.0
        self._consecutive_rejects = 0

        self.x = self.x + K @ y
        self.x[IDX_ATT] = np.array([wrap_pi(v) for v in self.x[IDX_ATT]])
        I_KH = np.eye(STATE_DIM) - K @ H
        self.P = I_KH @ self.P @ I_KH.T + K @ R @ K.T
        self._symmetrise()
        self.updates += 1
        norm = float(np.linalg.norm(y))
        self._innovations.append(norm)
        if len(self._innovations) > 200:
            del self._innovations[:-200]
        return norm

    # ------------------------------------------------------------------
    # sensor-specific updates
    # ------------------------------------------------------------------
    def update_position(self, position: Sequence[float], sigma: float | Sequence[float]) -> float:
        self._note_aiding()
        z = np.asarray(position, dtype=float)
        H = np.zeros((3, STATE_DIM))
        H[:, IDX_POS] = np.eye(3)
        R = _diag_from_sigma(sigma, 3)
        return self._update(z, self.x[IDX_POS].copy(), H, R, source="position")

    def update_lidar_pose(self, position: Sequence[float], yaw: float) -> float:
        """Scan-match update: planar position + heading from LiDAR registration.

        A 2D scan match observes ``(x, y, yaw)`` only. Feeding the filter's own
        ``z`` back as a pseudo-measurement would shrink the vertical covariance
        without adding information and let the altitude drift away unchecked,
        so the vertical channel is deliberately left to the altitude prior.
        """
        self._note_aiding()
        cfg = self.config
        yaw_innovation = wrap_pi(wrap_pi(yaw) - self.x[8])
        z = np.array([position[0], position[1], self.x[8] + yaw_innovation])
        h = np.array([self.x[0], self.x[1], self.x[8]])
        H = np.zeros((3, STATE_DIM))
        H[0, 0] = 1.0
        H[1, 1] = 1.0
        H[2, 8] = 1.0
        R = np.diag([cfg.sigma_lidar_pos ** 2, cfg.sigma_lidar_pos ** 2, cfg.sigma_yaw ** 2])
        return self._update(z, h, H, R, source="lidar_pose")

    def update_uwb_range(self, anchor: Sequence[float], distance: float, sigma: float | None = None) -> float:
        """Range-only update against a known UWB anchor position."""
        self._note_aiding()
        anchor_v = np.asarray(anchor, dtype=float)
        delta = self.x[IDX_POS] - anchor_v
        predicted = float(np.linalg.norm(delta))
        if predicted < 1e-6:
            return 0.0
        H = np.zeros((1, STATE_DIM))
        H[0, IDX_POS] = delta / predicted
        R = np.array([[(sigma or self.config.sigma_uwb_range) ** 2]])
        return self._update(np.array([float(distance)]), np.array([predicted]), H, R,
                            source=f"uwb:{anchor_v[0]:.1f},{anchor_v[1]:.1f}")

    def update_ble_position(self, position: Sequence[float], sigma: float | None = None) -> float:
        self._note_aiding()
        return self.update_position(position, sigma or self.config.sigma_ble_pos)

    def update_yaw(self, yaw: float, sigma: float | None = None) -> float:
        H = np.zeros((1, STATE_DIM))
        H[0, 8] = 1.0
        innovation = wrap_pi(wrap_pi(yaw) - self.x[8])
        z = np.array([self.x[8] + innovation])
        R = np.array([[(sigma or self.config.sigma_yaw) ** 2]])
        return self._update(z, np.array([self.x[8]]), H, R, source="yaw")

    def update_zero_velocity(self) -> float:
        """ZUPT - clamps drift while the operator stands still."""
        H = np.zeros((3, STATE_DIM))
        H[:, IDX_VEL] = np.eye(3)
        R = np.eye(3) * self.config.sigma_zupt ** 2
        return self._update(np.zeros(3), self.x[IDX_VEL].copy(), H, R, source="zupt")

    def update_altitude(self, altitude: float, sigma: float | None = None) -> float:
        self._note_aiding()
        H = np.zeros((1, STATE_DIM))
        H[0, 2] = 1.0
        R = np.array([[(sigma or self.config.sigma_baro_z) ** 2]])
        return self._update(np.array([float(altitude)]), np.array([self.x[2]]), H, R, source="altitude")

    # ------------------------------------------------------------------
    # diagnostics / serialisation
    # ------------------------------------------------------------------
    def _symmetrise(self) -> None:
        self.P = 0.5 * (self.P + self.P.T)
        np.fill_diagonal(self.P, np.maximum(np.diag(self.P), 1e-9))

    @property
    def innovation_rms(self) -> float:
        if not self._innovations:
            return 0.0
        arr = np.asarray(self._innovations[-50:])
        return float(math.sqrt(float(np.mean(arr * arr))))

    @property
    def position_sigma(self) -> np.ndarray:
        return np.sqrt(np.diag(self.P)[IDX_POS])

    def snapshot(self, timestamp: float) -> EkfSnapshot:
        R = self.rotation_matrix
        pos_sigma = self.position_sigma
        return EkfSnapshot(
            timestamp=timestamp,
            position=[float(v) for v in self.x[IDX_POS]],
            velocity=[float(v) for v in self.x[IDX_VEL]],
            orientation_euler=[float(v) for v in self.x[IDX_ATT]],
            orientation_quaternion=list(matrix_to_quaternion(R)),
            gyro_bias=[float(v) for v in self.x[IDX_BG]],
            accel_bias=[float(v) for v in self.x[IDX_BA]],
            covariance_diagonal=[float(v) for v in np.diag(self.P)],
            position_sigma=[float(v) for v in pos_sigma],
            trace=float(np.trace(self.P)),
            innovation_rms=self.innovation_rms,
            updates=self.updates,
            rejected=self.rejected,
            converged=bool(np.max(pos_sigma) < QUALITY_GOOD_M),
            quality=classify_quality(float(np.max(pos_sigma))),
            seconds_since_aiding=(
                float(self._clock - self.last_aiding_time)
                if self.last_aiding_time is not None else float("inf")
            ),
        )

    def transform(self) -> dict:
        """4x4 pose as a flat dict (matches the Android ``Transform3D``)."""
        R = self.rotation_matrix
        roll, pitch, yaw = self.x[IDX_ATT]
        return {
            "offset_x": float(self.x[0]),
            "offset_y": float(self.x[1]),
            "offset_z": float(self.x[2]),
            "roll": float(roll),
            "pitch": float(pitch),
            "yaw": float(yaw),
            "quaternion": list(matrix_to_quaternion(R)),
            "matrix": [float(v) for v in _pose_matrix(R, self.x[IDX_POS]).flatten(order="F")],
        }


def _pose_matrix(R: np.ndarray, t: np.ndarray) -> np.ndarray:
    m = np.eye(4)
    m[:3, :3] = R
    m[:3, 3] = t
    return m


def _diag_from_sigma(sigma: float | Sequence[float], n: int) -> np.ndarray:
    if isinstance(sigma, (int, float)):
        return np.eye(n) * float(sigma) ** 2
    arr = np.asarray(list(sigma), dtype=float)
    return np.diag(arr ** 2)


def _d_rotation_d_euler(roll: float, pitch: float, yaw: float, a_body: np.ndarray) -> np.ndarray:
    """Jacobian d(R(euler) @ a_body) / d(euler) -- 3x3, computed analytically."""
    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    ax, ay, az = float(a_body[0]), float(a_body[1]), float(a_body[2])

    d_roll = np.array(
        [
            (cy * sp * cr + sy * sr) * ay + (-cy * sp * sr + sy * cr) * az,
            (sy * sp * cr - cy * sr) * ay + (-sy * sp * sr - cy * cr) * az,
            (cp * cr) * ay + (-cp * sr) * az,
        ]
    )
    d_pitch = np.array(
        [
            (-cy * sp) * ax + (cy * cp * sr) * ay + (cy * cp * cr) * az,
            (-sy * sp) * ax + (sy * cp * sr) * ay + (sy * cp * cr) * az,
            (-cp) * ax + (-sp * sr) * ay + (-sp * cr) * az,
        ]
    )
    d_yaw = np.array(
        [
            (-sy * cp) * ax + (-sy * sp * sr - cy * cr) * ay + (-sy * sp * cr + cy * sr) * az,
            (cy * cp) * ax + (cy * sp * sr - sy * cr) * ay + (cy * sp * cr + sy * sr) * az,
            0.0,
        ]
    )
    return np.column_stack([d_roll, d_pitch, d_yaw])


@dataclass
class FusionDiagnostics:
    """Rolling quality metrics exposed on ``/api/v1/agent/state``."""

    dropped_samples: int = 0
    # Pipeline-level refusals (stuck driver, BLE fix too far from the estimate,
    # a tick that raised). Distinct from ``EkfSnapshot.rejected``, which counts
    # measurements the filter's own chi-square gate turned away.
    rejected_updates: int = 0
    sources: dict[str, int] = field(default_factory=dict)

    def note(self, source: str) -> None:
        self.sources[source] = self.sources.get(source, 0) + 1

    def as_dict(self) -> dict:
        return {
            "dropped_samples": self.dropped_samples,
            "rejected_updates": self.rejected_updates,
            "sources": dict(self.sources),
        }


