"""IMU driver: gyro + accelerometer + magnetometer.

On the CT45P the IMU is read by the Android app and streamed to the agent;
this driver covers the standalone case (external IMU on serial) and the
simulator that walks a virtual operator through the building.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

import numpy as np

from ..world import WORLD, World
from .base import SensorDriver

MAG_DECLINATION = 0.0  # rad, site-specific
MAG_FIELD_NT = 48.0


@dataclass
class ImuSample:
    """One IMU epoch in the body frame."""

    timestamp: float
    gyro: tuple[float, float, float]     # rad/s
    accel: tuple[float, float, float]    # m/s^2, specific force
    mag: tuple[float, float, float]      # uT
    temperature: float = 32.0

    def as_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "gyro": [round(v, 5) for v in self.gyro],
            "accel": [round(v, 4) for v in self.accel],
            "mag": [round(v, 3) for v in self.mag],
            "temperature": round(self.temperature, 2),
        }

    @property
    def is_static(self) -> bool:
        """Instantaneous stillness hint.

        Note: a single sample is a *weak* indicator - during the stance phase
        of a normal stride the accelerometer also reads ~1 g. Use
        :class:`StaticDetector` for the actual ZUPT trigger; this property is
        only a cheap pre-filter.
        """
        acc_mag = math.sqrt(sum(v * v for v in self.accel))
        gyro_mag = math.sqrt(sum(v * v for v in self.gyro))
        return abs(acc_mag - 9.80665) < 0.25 and gyro_mag < 0.05

    def heading(self) -> float:
        """Tilt-naive magnetic heading (the EKF refines it)."""
        return math.atan2(-self.mag[1], self.mag[0]) + MAG_DECLINATION


class StaticDetector:
    """Windowed zero-velocity detector.

    A ZUPT must only fire when the operator is *actually* standing still.
    A single-sample test is fooled by the stance phase of every stride, which
    silently clamps the EKF velocity and makes the estimate lag behind reality,
    so we require a whole window of low variance instead.
    """

    def __init__(self, window: int = 12, accel_var_max: float = 0.09, gyro_max: float = 0.06) -> None:
        self.window = window
        self.accel_var_max = accel_var_max
        self.gyro_max = gyro_max
        self._accel: list[float] = []
        self._gyro: list[float] = []

    def push(self, sample: "ImuSample") -> bool:
        acc_mag = math.sqrt(sum(v * v for v in sample.accel))
        gyro_mag = math.sqrt(sum(v * v for v in sample.gyro))
        self._accel.append(acc_mag)
        self._gyro.append(gyro_mag)
        if len(self._accel) > self.window:
            del self._accel[0]
            del self._gyro[0]
        if len(self._accel) < self.window:
            return False
        accel_var = float(np.var(self._accel))
        gyro_peak = float(np.max(self._gyro))
        mean_offset = abs(float(np.mean(self._accel)) - 9.80665)
        return accel_var < self.accel_var_max and gyro_peak < self.gyro_max and mean_offset < 0.4

    def reset(self) -> None:
        self._accel.clear()
        self._gyro.clear()


class ImuManagerPath:
    """Deterministic walking path for the virtual operator."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.waypoints = [
            (2.5, 2.5), (8.0, 2.5), (10.0, 6.5), (3.6, 6.5),
            (3.6, 10.5), (10.0, 6.8), (15.6, 6.5), (15.6, 10.5),
            (10.0, 6.5), (2.5, 2.5),
        ]
        self.index = 0
        self.position = np.array(self.waypoints[0], dtype=float)
        self.yaw = 0.0
        self.speed = 0.85
        self.paused_until = 0.0

    def step(self, dt: float) -> tuple[np.ndarray, float, np.ndarray]:
        """Advance the operator. Returns ``(position, yaw, velocity)``."""
        now = time.time()
        if now < self.paused_until:
            return self.position.copy(), self.yaw, np.zeros(2)
        target = np.array(self.waypoints[(self.index + 1) % len(self.waypoints)], dtype=float)
        delta = target - self.position
        dist = float(np.linalg.norm(delta))
        if dist < 0.25:
            self.index = (self.index + 1) % len(self.waypoints)
            self.paused_until = now + 1.2   # dwell -> gives the EKF a ZUPT
            return self.position.copy(), self.yaw, np.zeros(2)
        direction = delta / dist
        desired_yaw = math.atan2(direction[1], direction[0])
        yaw_err = math.atan2(math.sin(desired_yaw - self.yaw), math.cos(desired_yaw - self.yaw))
        self.yaw += float(np.clip(yaw_err, -1.8 * dt, 1.8 * dt))
        step = min(self.speed * dt, dist)
        velocity = direction * (step / max(dt, 1e-6))
        self.position = self.position + direction * step
        return self.position.copy(), self.yaw, velocity


class ImuDriver(SensorDriver):
    """Serial IMU (or a simulated 6-DOF walk through :mod:`aura.world`)."""

    name = "imu"

    def __init__(self, port: str = "", baud: int = 115200, simulate: bool = True, world: World | None = None) -> None:
        super().__init__(port, baud, simulate)
        self.world = world or WORLD
        self.path = ImuManagerPath(self.world)
        self._rng = np.random.default_rng(41)
        self._last = time.time()
        self._prev_velocity = np.zeros(2)
        self._bias_gyro = self._rng.normal(0.0, 0.004, 3)
        self._bias_accel = self._rng.normal(0.0, 0.03, 3)
        self._buffer = bytearray()
        self.external_pose: tuple[float, float, float] | None = None

    # ------------------------------------------------------------------
    def read(self) -> ImuSample | None:
        if self.simulate:
            return self._simulate()
        return self._read_hardware()  # pragma: no cover - hardware only

    def _simulate(self) -> ImuSample:
        now = time.time()
        dt = min(max(now - self._last, 1e-3), 0.25)
        self._last = now
        position, yaw, velocity = self.path.step(dt)
        # Differentiating velocity blows up on the very first sample (and after
        # any scheduler stall), so clamp to what a walking human can actually
        # produce - otherwise the EKF receives a 100 g impulse at startup.
        accel_world = np.clip((velocity - self._prev_velocity) / dt, -6.0, 6.0)
        self._prev_velocity = velocity
        yaw_rate = 0.0
        if abs(dt) > 1e-6:
            yaw_rate = float(np.clip((yaw - getattr(self, "_prev_yaw", yaw)) / dt, -3.0, 3.0))
        self._prev_yaw = yaw

        # walking gait: vertical bounce + slight roll
        gait = 2.0 * math.pi * 1.9 * now
        bounce = 0.55 * math.sin(gait) if np.linalg.norm(velocity) > 0.05 else 0.0
        roll = 0.035 * math.sin(gait / 2.0)
        pitch = 0.02 * math.cos(gait / 2.0)

        c, s = math.cos(yaw), math.sin(yaw)
        ax_body = accel_world[0] * c + accel_world[1] * s
        ay_body = -accel_world[0] * s + accel_world[1] * c

        accel = (
            ax_body + 9.80665 * math.sin(pitch) + self._bias_accel[0] + float(self._rng.normal(0, 0.07)),
            ay_body - 9.80665 * math.sin(roll) + self._bias_accel[1] + float(self._rng.normal(0, 0.07)),
            9.80665 + bounce + self._bias_accel[2] + float(self._rng.normal(0, 0.09)),
        )
        gyro = (
            0.07 * math.cos(gait / 2.0) + self._bias_gyro[0] + float(self._rng.normal(0, 0.008)),
            -0.05 * math.sin(gait / 2.0) + self._bias_gyro[1] + float(self._rng.normal(0, 0.008)),
            yaw_rate + self._bias_gyro[2] + float(self._rng.normal(0, 0.006)),
        )
        mag = (
            MAG_FIELD_NT * math.cos(yaw) + float(self._rng.normal(0, 0.8)),
            -MAG_FIELD_NT * math.sin(yaw) + float(self._rng.normal(0, 0.8)),
            -12.0 + float(self._rng.normal(0, 0.6)),
        )
        self.external_pose = (float(position[0]), float(position[1]), yaw)
        self._mark()
        return ImuSample(now, gyro, accel, mag, temperature=32.0 + 3.0 * math.sin(now / 90.0))

    def _read_hardware(self) -> ImuSample | None:  # pragma: no cover - hardware only
        chunk = self._read(512)
        if chunk:
            self._buffer.extend(chunk)
        if b"\n" not in self._buffer:
            return None
        line, _, rest = bytes(self._buffer).partition(b"\n")
        self._buffer = bytearray(rest)
        # CSV: gx,gy,gz,ax,ay,az,mx,my,mz[,temp]
        parts = line.decode("ascii", "ignore").strip().split(",")
        if len(parts) < 9:
            return None
        try:
            values = [float(p) for p in parts[:10]]
        except ValueError:
            self.status.errors += 1
            return None
        temp = values[9] if len(values) > 9 else 32.0
        self._mark()
        return ImuSample(
            time.time(),
            (values[0], values[1], values[2]),
            (values[3], values[4], values[5]),
            (values[6], values[7], values[8]),
            temp,
        )

    def ground_truth(self) -> tuple[float, float, float] | None:
        """Simulator-only: the true operator pose (used to drive other sims)."""
        return self.external_pose
