"""TI IWR6843 mmWave driver: TLV parser + synthetic target generator."""

from __future__ import annotations

import math
import struct
import time
from dataclasses import dataclass

import numpy as np

from ..world import WORLD, World
from .base import SensorDriver

MAGIC_WORD = bytes([0x02, 0x01, 0x04, 0x03, 0x06, 0x05, 0x08, 0x07])
TLV_DETECTED_POINTS = 1
TLV_RANGE_PROFILE = 2
TLV_SIDE_INFO = 7

MMWAVE_FULL = "\n".join(
    [
        "sensorStop",
        "flushCfg",
        "dfeDataOutputMode 1",
        "channelCfg 15 7 0",
        "adcCfg 2 1",
        "adcbufCfg -1 0 1 1 1",
        "profileCfg 0 60 359 7 57.14 0 0 70 1 256 5209 0 0 158",
        "chirpCfg 0 0 0 0 0 0 0 1",
        "chirpCfg 1 1 0 0 0 0 0 4",
        "chirpCfg 2 2 0 0 0 0 0 2",
        "frameCfg 0 2 16 0 100 1 0",
        "lowPower 0 0",
        "guiMonitor -1 1 0 0 0 0 1",
        "cfarCfg -1 0 2 8 4 3 0 15 1",
        "cfarCfg -1 1 0 4 2 3 1 15 1",
        "multiObjBeamForming -1 1 0.5",
        "clutterRemoval -1 1",
        "calibDcRangeSig -1 0 -5 8 256",
        "compRangeBiasAndRxChanPhase 0.0 1 0 1 0 1 0 1 0 1 0 1 0",
        "sensorStart",
        "",
    ]
)

MMWAVE_REDUCED = "\n".join(
    [
        "sensorStop",
        "flushCfg",
        "dfeDataOutputMode 1",
        "channelCfg 7 3 0",
        "adcCfg 2 1",
        "adcbufCfg -1 0 1 1 1",
        "profileCfg 0 60 359 7 57.14 0 0 40 1 128 3000 0 0 158",
        "chirpCfg 0 0 0 0 0 0 0 1",
        "chirpCfg 1 1 0 0 0 0 0 2",
        "frameCfg 0 1 8 0 250 1 0",
        "lowPower 0 1",
        "guiMonitor -1 1 0 0 0 0 0",
        "cfarCfg -1 0 2 8 4 3 0 15 1",
        "clutterRemoval -1 1",
        "sensorStart",
        "",
    ]
)


@dataclass
class MmwaveTarget:
    """A detected point/target in the sensor body frame."""

    x: float
    y: float
    z: float
    velocity: float          # radial, m/s (Doppler)
    snr: float = 0.0
    track_id: int = -1

    @property
    def range_m(self) -> float:
        return math.sqrt(self.x * self.x + self.y * self.y + self.z * self.z)

    @property
    def azimuth(self) -> float:
        return math.atan2(self.y, self.x)

    def as_dict(self) -> dict:
        return {
            "x": round(self.x, 3),
            "y": round(self.y, 3),
            "z": round(self.z, 3),
            "velocity": round(self.velocity, 3),
            "snr": round(self.snr, 1),
            "track_id": self.track_id,
            "range": round(self.range_m, 3),
            "azimuth": round(self.azimuth, 4),
        }


class MmwaveDriver(SensorDriver):
    """IWR6843 over the CLI+DATA UART pair."""

    name = "mmwave"

    def __init__(
        self,
        port: str = "",
        baud: int = 921600,
        simulate: bool = True,
        reduced: bool = False,
        world: World | None = None,
    ) -> None:
        super().__init__(port, baud, simulate)
        self.reduced = reduced
        self.world = world or WORLD
        self._buffer = bytearray()
        self._rng = np.random.default_rng(11)
        self._pose = (2.0, 2.0, 0.0)
        self._people: list[np.ndarray] = []
        self.max_range = 12.0

    def open(self) -> bool:
        ok = super().open()
        self.configure_profile(self.reduced)
        self.status.extra["profile"] = "reduced" if self.reduced else "full"
        return ok

    def configure_profile(self, reduced: bool) -> None:
        """Push a chirp profile over the CLI UART (no-op in simulation)."""
        self.reduced = reduced
        self.status.extra["profile"] = "reduced" if reduced else "full"
        if self.simulate:
            return
        cfg = MMWAVE_REDUCED if reduced else MMWAVE_FULL  # pragma: no cover
        for line in cfg.splitlines():  # pragma: no cover - hardware only
            if line.strip():
                self._write((line + "\n").encode())
                time.sleep(0.01)

    def set_pose(self, x: float, y: float, yaw: float) -> None:
        self._pose = (float(x), float(y), float(yaw))

    def set_people(self, people: list[np.ndarray]) -> None:
        """Ground-truth people positions injected by the scenario engine."""
        self._people = [np.asarray(p, dtype=float) for p in people]

    # ------------------------------------------------------------------
    def read(self) -> list[MmwaveTarget]:
        if self.simulate:
            return self._simulate()
        return self._read_hardware()  # pragma: no cover - hardware only

    def _simulate(self) -> list[MmwaveTarget]:
        px, py, yaw = self._pose
        targets: list[MmwaveTarget] = []
        t = self.elapsed
        # 1) moving people (strong Doppler)
        for idx, person in enumerate(self._people):
            dx = float(person[0]) - px
            dy = float(person[1]) - py
            dist = math.hypot(dx, dy)
            if dist > self.max_range or not self.world.visible((px, py), (person[0], person[1])):
                continue
            bearing = math.atan2(dy, dx) - yaw
            if abs(math.atan2(math.sin(bearing), math.cos(bearing))) > math.radians(60):
                continue
            speed = float(person[2]) if len(person) > 2 else 0.9
            targets.append(
                MmwaveTarget(
                    x=dist * math.cos(bearing) + float(self._rng.normal(0, 0.05)),
                    y=dist * math.sin(bearing) + float(self._rng.normal(0, 0.05)),
                    z=float(self._rng.normal(0.0, 0.08)),
                    velocity=speed * math.cos(bearing) + float(self._rng.normal(0, 0.06)),
                    snr=float(self._rng.uniform(14, 28)),
                    track_id=idx,
                )
            )
        # 2) static clutter from walls inside the field of view
        n_clutter = 6 if self.reduced else 14
        for _ in range(n_clutter):
            bearing = float(self._rng.uniform(-math.pi / 3, math.pi / 3))
            dist = self.world.raycast((px, py), yaw + bearing, self.max_range)
            if dist >= self.max_range:
                continue
            dist += float(self._rng.normal(0, 0.03))
            targets.append(
                MmwaveTarget(
                    x=dist * math.cos(bearing),
                    y=dist * math.sin(bearing),
                    z=float(self._rng.normal(0.2, 0.25)),
                    velocity=float(self._rng.normal(0.0, 0.03)),
                    snr=float(self._rng.uniform(5, 14)),
                )
            )
        # 3) micro-Doppler of the operator's own breathing chest wall
        targets.append(
            MmwaveTarget(
                x=0.45,
                y=0.0,
                z=0.0,
                velocity=0.012 * math.sin(2 * math.pi * 0.28 * t),
                snr=32.0,
                track_id=-2,
            )
        )
        self._mark()
        return targets

    def _read_hardware(self) -> list[MmwaveTarget]:  # pragma: no cover - hardware only
        chunk = self._read(8192)
        if chunk:
            self._buffer.extend(chunk)
        targets: list[MmwaveTarget] = []
        while True:
            idx = self._buffer.find(MAGIC_WORD)
            if idx < 0:
                if len(self._buffer) > 65536:
                    del self._buffer[:-8]
                break
            if idx:
                del self._buffer[:idx]
            if len(self._buffer) < 40:
                break
            header = struct.unpack("<8I", bytes(self._buffer[8:40]))
            total_len = header[2]
            num_tlvs = header[6]
            if total_len < 40 or total_len > 1 << 20:
                del self._buffer[:8]
                continue
            if len(self._buffer) < total_len:
                break
            frame = bytes(self._buffer[:total_len])
            del self._buffer[:total_len]
            offset = 40
            for _ in range(num_tlvs):
                if offset + 8 > len(frame):
                    break
                tlv_type, tlv_len = struct.unpack("<2I", frame[offset : offset + 8])
                payload = frame[offset + 8 : offset + tlv_len]
                offset += tlv_len
                if tlv_type == TLV_DETECTED_POINTS:
                    count = len(payload) // 16
                    for i in range(count):
                        x, y, z, v = struct.unpack("<4f", payload[i * 16 : (i + 1) * 16])
                        targets.append(MmwaveTarget(x, y, z, v))
                elif tlv_type == TLV_SIDE_INFO and targets:
                    count = min(len(payload) // 4, len(targets))
                    for i in range(count):
                        snr, _noise = struct.unpack("<2h", payload[i * 4 : (i + 1) * 4])
                        targets[-count + i].snr = snr / 10.0
        if targets:
            self._mark()
        return targets
