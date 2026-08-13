"""RPLIDAR A1/A2/S2 driver (EXPRESS_SCAN) plus a ray-cast simulator."""

from __future__ import annotations

import math
import struct
import time
from dataclasses import dataclass, field

import numpy as np

from ..world import WORLD, World
from .base import SensorDriver

SYNC_BYTE = 0xA5
CMD_STOP = 0x25
CMD_RESET = 0x40
CMD_SCAN = 0x20
CMD_EXPRESS_SCAN = 0x82
CMD_GET_INFO = 0x50
CMD_GET_HEALTH = 0x52


@dataclass
class LidarScan:
    """One sweep: polar samples in the sensor body frame."""

    timestamp: float
    angles: list[float] = field(default_factory=list)      # rad, body frame
    distances: list[float] = field(default_factory=list)   # metres
    quality: list[int] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.angles)

    def to_cartesian(self, pose: tuple[float, float, float]) -> np.ndarray:
        """Project the sweep into the map frame given ``(x, y, yaw)``."""
        if not self.angles:
            return np.zeros((0, 2))
        px, py, yaw = pose
        a = np.asarray(self.angles) + yaw
        d = np.asarray(self.distances)
        return np.column_stack([px + d * np.cos(a), py + d * np.sin(a)])

    def as_dict(self, decimate: int = 1) -> dict:
        step = max(1, int(decimate))
        return {
            "timestamp": self.timestamp,
            "angles": [round(v, 4) for v in self.angles[::step]],
            "distances": [round(v, 3) for v in self.distances[::step]],
            "count": len(self.angles),
        }


class LidarDriver(SensorDriver):
    """Reads an RPLIDAR over USB-serial; falls back to a synthetic sweep."""

    name = "lidar"

    def __init__(
        self,
        port: str = "",
        baud: int = 115200,
        simulate: bool = True,
        world: World | None = None,
        beams: int = 240,
        max_range: float = 16.0,
    ) -> None:
        super().__init__(port, baud, simulate)
        self.world = world or WORLD
        self.beams = beams
        self.max_range = max_range
        self._buffer = bytearray()
        self._pose = (2.0, 2.0, 0.0)
        self._rng = np.random.default_rng(7)
        self._scan_started = False

    # ------------------------------------------------------------------
    def open(self) -> bool:
        ok = super().open()
        if ok and not self.simulate:  # pragma: no cover - hardware only
            self._write(bytes([SYNC_BYTE, CMD_STOP]))
            time.sleep(0.05)
            self._write(bytes([SYNC_BYTE, CMD_RESET]))
            time.sleep(0.8)
            self._start_express_scan()
        self.status.extra["beams"] = self.beams
        self.status.extra["max_range_m"] = self.max_range
        return ok

    def _start_express_scan(self) -> None:  # pragma: no cover - hardware only
        payload = bytes([0x05, 0x00, 0x00, 0x00, 0x00])
        checksum = 0
        frame = bytes([SYNC_BYTE, CMD_EXPRESS_SCAN, len(payload)]) + payload
        for b in frame:
            checksum ^= b
        self._write(frame + bytes([checksum]))
        self._scan_started = True

    def set_pose(self, x: float, y: float, yaw: float) -> None:
        """Ground-truth pose used by the simulator (ignored on hardware)."""
        self._pose = (float(x), float(y), float(yaw))

    # ------------------------------------------------------------------
    def read(self) -> LidarScan | None:
        if self.simulate:
            return self._simulate()
        return self._read_hardware()  # pragma: no cover - hardware only

    def _simulate(self) -> LidarScan:
        x, y, yaw = self._pose
        sweep = self.world.scan((x, y), yaw, beams=self.beams, max_range=self.max_range)
        noise = self._rng.normal(0.0, 0.012, size=len(sweep))
        distances = sweep[:, 1] + noise
        # dropouts on dark/specular surfaces and beyond range
        keep = (distances < self.max_range - 0.05) & (self._rng.random(len(distances)) > 0.04)
        angles = sweep[keep, 0]
        dist = np.clip(distances[keep], 0.05, self.max_range)
        quality = (self._rng.integers(28, 60, size=len(dist))).tolist()
        self._mark()
        return LidarScan(
            timestamp=time.time(),
            angles=[float(v) for v in angles],
            distances=[float(v) for v in dist],
            quality=[int(q) for q in quality],
        )

    def _read_hardware(self) -> LidarScan | None:  # pragma: no cover - hardware only
        if not self._scan_started:
            self._start_express_scan()
        chunk = self._read(4096)
        if chunk:
            self._buffer.extend(chunk)
        angles: list[float] = []
        distances: list[float] = []
        quality: list[int] = []
        # Legacy 5-byte measurement nodes (also emitted while express warms up)
        while len(self._buffer) >= 5:
            b0 = self._buffer[0]
            start = b0 & 0x01
            inverted = (b0 >> 1) & 0x01
            if start == inverted:  # S and !S must differ -> resync
                del self._buffer[0]
                continue
            if not (self._buffer[1] & 0x01):
                del self._buffer[0]
                continue
            node = bytes(self._buffer[:5])
            del self._buffer[:5]
            qual = node[0] >> 2
            angle_q6 = ((node[2] << 7) | (node[1] >> 1)) & 0x7FFF
            dist_q2 = struct.unpack("<H", node[3:5])[0]
            if dist_q2 == 0:
                continue
            angle_deg = angle_q6 / 64.0
            distance_m = dist_q2 / 4000.0
            if distance_m > self.max_range:
                continue
            angles.append(math.radians(angle_deg) - math.pi)
            distances.append(distance_m)
            quality.append(qual)
        if not angles:
            return None
        self._mark()
        return LidarScan(time.time(), angles, distances, quality)

    def stop(self) -> None:  # pragma: no cover - hardware only
        if not self.simulate:
            self._write(bytes([SYNC_BYTE, CMD_STOP]))
        self._scan_started = False

    def close(self) -> None:
        self.stop() if not self.simulate else None
        super().close()
