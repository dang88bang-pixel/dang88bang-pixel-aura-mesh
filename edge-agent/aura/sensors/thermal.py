"""Thermal camera driver (FLIR Lepton 3.5 / MLX90640 class sensors)."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

import numpy as np

from ..world import WORLD, World
from .base import SensorDriver


@dataclass
class ThermalFrame:
    """A low-resolution radiometric frame in degrees Celsius."""

    timestamp: float
    width: int
    height: int
    data: np.ndarray
    fov_deg: float = 55.0

    @property
    def max_temp(self) -> float:
        return float(self.data.max())

    @property
    def mean_temp(self) -> float:
        return float(self.data.mean())

    def hotspots(self, threshold: float = 30.0) -> list[dict]:
        """Connected-ish hotspot extraction (column clustering, no SciPy)."""
        mask = self.data > threshold
        if not mask.any():
            return []
        spots: list[dict] = []
        cols = np.where(mask.any(axis=0))[0]
        groups: list[list[int]] = []
        for c in cols:
            if groups and c - groups[-1][-1] <= 1:
                groups[-1].append(int(c))
            else:
                groups.append([int(c)])
        for g in groups:
            sub = self.data[:, g[0] : g[-1] + 1]
            sub_mask = sub > threshold
            if not sub_mask.any():
                continue
            rows = np.where(sub_mask.any(axis=1))[0]
            cx = (g[0] + g[-1]) / 2.0
            cy = float(rows.mean())
            bearing = math.radians((cx / max(1, self.width - 1) - 0.5) * self.fov_deg)
            spots.append(
                {
                    "center_x": round(cx, 2),
                    "center_y": round(cy, 2),
                    "bearing": round(bearing, 4),
                    "peak_temp": round(float(sub.max()), 2),
                    "pixels": int(sub_mask.sum()),
                }
            )
        return spots

    def as_dict(self, include_data: bool = False) -> dict:
        payload = {
            "timestamp": self.timestamp,
            "width": self.width,
            "height": self.height,
            "max_temp": round(self.max_temp, 2),
            "mean_temp": round(self.mean_temp, 2),
            "hotspots": self.hotspots(),
        }
        if include_data:
            payload["data"] = [round(float(v), 1) for v in self.data.flatten()]
        return payload


class ThermalDriver(SensorDriver):
    """32x24 thermal array; simulates body heat and (during fire scenarios) smoke."""

    name = "thermal"

    def __init__(self, port: str = "", simulate: bool = True, world: World | None = None,
                 width: int = 32, height: int = 24) -> None:
        super().__init__(port, 921600, simulate)
        self.world = world or WORLD
        self.width = width
        self.height = height
        self._rng = np.random.default_rng(53)
        self._pose = (2.0, 2.0, 0.0)
        self._people: list[np.ndarray] = []
        self._ambient = 21.5
        self._fire: tuple[float, float, float] | None = None
        self.fov_deg = 55.0

    def set_pose(self, x: float, y: float, yaw: float) -> None:
        self._pose = (float(x), float(y), float(yaw))

    def set_people(self, people: list[np.ndarray]) -> None:
        self._people = [np.asarray(p, dtype=float) for p in people]

    def set_fire(self, source: tuple[float, float, float] | None) -> None:
        """``(x, y, intensity)`` heat source, or ``None``."""
        self._fire = source

    def read(self) -> ThermalFrame | None:
        if self.simulate:
            return self._simulate()
        return self._read_hardware()  # pragma: no cover - hardware only

    def _simulate(self) -> ThermalFrame:
        px, py, yaw = self._pose
        frame = np.full((self.height, self.width), self._ambient, dtype=float)
        frame += self._rng.normal(0.0, 0.25, frame.shape)
        half_fov = math.radians(self.fov_deg / 2.0)

        def project(tx: float, ty: float) -> int | None:
            bearing = math.atan2(ty - py, tx - px) - yaw
            bearing = math.atan2(math.sin(bearing), math.cos(bearing))
            if abs(bearing) > half_fov:
                return None
            return int((bearing / (2 * half_fov) + 0.5) * (self.width - 1))

        yy, xx = np.mgrid[0 : self.height, 0 : self.width]
        for person in self._people:
            tx, ty = float(person[0]), float(person[1])
            if not self.world.visible((px, py), (tx, ty)):
                continue
            col = project(tx, ty)
            if col is None:
                continue
            dist = max(0.6, math.hypot(tx - px, ty - py))
            sigma_x = max(1.2, 9.0 / dist)
            sigma_y = max(2.0, 14.0 / dist)
            amp = 12.5 / (1.0 + 0.09 * dist * dist)
            frame += amp * np.exp(
                -(((xx - col) ** 2) / (2 * sigma_x ** 2) + ((yy - self.height * 0.55) ** 2) / (2 * sigma_y ** 2))
            )
        if self._fire is not None:
            fx, fy, intensity = self._fire
            col = project(fx, fy)
            if col is not None and self.world.visible((px, py), (fx, fy)):
                dist = max(0.8, math.hypot(fx - px, fy - py))
                amp = 180.0 * intensity / (1.0 + 0.2 * dist * dist)
                flicker = 1.0 + 0.12 * math.sin(self.elapsed * 11.0)
                frame += amp * flicker * np.exp(
                    -(((xx - col) ** 2) / 18.0 + ((yy - self.height * 0.7) ** 2) / 26.0)
                )
        self._mark()
        return ThermalFrame(time.time(), self.width, self.height, frame, self.fov_deg)

    def _read_hardware(self) -> ThermalFrame | None:  # pragma: no cover - hardware only
        expected = self.width * self.height * 2
        raw = self._read(expected)
        if len(raw) < expected:
            return None
        data = np.frombuffer(raw[:expected], dtype="<u2").astype(float)
        # MLX90640 raw -> Celsius (0.01 K steps)
        frame = (data * 0.01 - 273.15).reshape(self.height, self.width)
        self._mark()
        return ThermalFrame(time.time(), self.width, self.height, frame, self.fov_deg)
