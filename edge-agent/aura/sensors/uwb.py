"""Qorvo DWM3000 UWB driver.

Provides two things the rest of the stack cares about:

* **Two-way ranging** to fixed anchors (feeds the EKF range update).
* **Channel-impulse-response micro-Doppler**, i.e. through-wall detection of
  breathing/heartbeat by watching the slow phase modulation of a static
  multipath tap. The FFT analysis lives in :mod:`aura.doppler`.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

import numpy as np

from ..world import DEFAULT_UWB_ANCHORS, WORLD, World
from .base import SensorDriver

# DW3000 register shorthand used by the hardware path
CMD_RANGE = b"$RANGE\r\n"
CMD_CIR = b"$CIR\r\n"


@dataclass
class UwbReading:
    """One UWB epoch."""

    timestamp: float
    ranges: dict[str, float] = field(default_factory=dict)      # anchor -> metres
    cir_amplitude: float = 0.0                                  # normalised tap power
    cir_phase: float = 0.0                                      # rad
    los: dict[str, bool] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "ranges": {k: round(v, 3) for k, v in self.ranges.items()},
            "cir_amplitude": round(self.cir_amplitude, 5),
            "cir_phase": round(self.cir_phase, 5),
            "los": self.los,
        }


class UwbDriver(SensorDriver):
    """DWM3000 over serial (or a physics-flavoured simulator)."""

    name = "uwb"

    def __init__(
        self,
        port: str = "",
        baud: int = 115200,
        simulate: bool = True,
        world: World | None = None,
        anchors: dict[str, tuple[float, float, float]] | None = None,
    ) -> None:
        super().__init__(port, baud, simulate)
        self.world = world or WORLD
        self.anchors = dict(anchors or DEFAULT_UWB_ANCHORS)
        self._rng = np.random.default_rng(23)
        self._pose = (2.0, 2.0, 0.0)
        self._hidden_subjects: list[tuple[float, float, float, float]] = []
        self._buffer = bytearray()

    def open(self) -> bool:
        ok = super().open()
        self.status.extra["anchors"] = list(self.anchors)
        return ok

    def set_pose(self, x: float, y: float, yaw: float) -> None:
        self._pose = (float(x), float(y), float(yaw))

    def set_hidden_subjects(self, subjects: list[tuple[float, float, float, float]]) -> None:
        """``(x, y, respiration_hz, heart_hz)`` for through-wall subjects."""
        self._hidden_subjects = list(subjects)

    # ------------------------------------------------------------------
    def read(self) -> UwbReading | None:
        if self.simulate:
            return self._simulate()
        return self._read_hardware()  # pragma: no cover - hardware only

    def _simulate(self) -> UwbReading:
        px, py, _yaw = self._pose
        t = time.time()
        ranges: dict[str, float] = {}
        los: dict[str, bool] = {}
        for name, (ax, ay, az) in self.anchors.items():
            true_range = math.sqrt((px - ax) ** 2 + (py - ay) ** 2 + (1.4 - az) ** 2)
            clear = self.world.visible((px, py), (ax, ay))
            walls = 0 if clear else self.world.wall_count_between((px, py), (ax, ay))
            # NLOS bias: each wall adds ~15 cm of excess delay
            bias = 0.15 * walls
            noise = self._rng.normal(0.0, 0.05 if clear else 0.16)
            ranges[name] = max(0.05, true_range + bias + noise)
            los[name] = clear

        # channel impulse response: sum of subject chest-wall modulations
        amplitude = 0.0
        phase = 0.0
        for sx, sy, resp_hz, heart_hz in self._hidden_subjects:
            dist = math.hypot(sx - px, sy - py)
            if dist > 9.0:
                continue
            walls = self.world.wall_count_between((px, py), (sx, sy))
            attenuation = math.exp(-0.55 * walls) / (1.0 + 0.28 * dist * dist)
            chest = 0.006 * math.sin(2 * math.pi * resp_hz * t)
            heart = 0.0007 * math.sin(2 * math.pi * heart_hz * t)
            amplitude += attenuation * (1.0 + 60.0 * (chest + heart))
            phase += attenuation * (chest + heart) * 4.0 * math.pi / 0.0417  # lambda @ 7.2 GHz
        amplitude += float(self._rng.normal(0.0, 0.004))
        phase += float(self._rng.normal(0.0, 0.02))
        self._mark()
        return UwbReading(t, ranges, amplitude, phase, los)

    def _read_hardware(self) -> UwbReading | None:  # pragma: no cover - hardware only
        self._write(CMD_RANGE)
        chunk = self._read(2048)
        if chunk:
            self._buffer.extend(chunk)
        if b"\n" not in self._buffer:
            return None
        line, _, rest = bytes(self._buffer).partition(b"\n")
        self._buffer = bytearray(rest)
        text = line.decode("ascii", "ignore").strip()
        if not text:
            return None
        ranges: dict[str, float] = {}
        amplitude = 0.0
        phase = 0.0
        # Expected DWM3000 shell format: "ANCHOR-A=3.214,ANCHOR-B=7.882;CIR=0.42,1.87"
        head, _, cir = text.partition(";")
        for token in head.split(","):
            if "=" not in token:
                continue
            key, _, value = token.partition("=")
            try:
                ranges[key.strip()] = float(value)
            except ValueError:
                continue
        if cir.startswith("CIR="):
            parts = cir[4:].split(",")
            try:
                amplitude = float(parts[0])
                phase = float(parts[1]) if len(parts) > 1 else 0.0
            except ValueError:
                pass
        if not ranges and amplitude == 0.0:
            return None
        self._mark()
        return UwbReading(time.time(), ranges, amplitude, phase, {k: True for k in ranges})

    # ------------------------------------------------------------------
    def multilaterate(self, ranges: dict[str, float], seed: tuple[float, float] | None = None) -> tuple[float, float] | None:
        """Least-squares 2D trilateration from >= 3 anchor ranges."""
        usable = [(self.anchors[k], v) for k, v in ranges.items() if k in self.anchors]
        if len(usable) < 3:
            return None
        (ax0, ay0, _az0), r0 = usable[0]
        A = []
        b = []
        for (ax, ay, _az), r in usable[1:]:
            A.append([2.0 * (ax - ax0), 2.0 * (ay - ay0)])
            b.append(r0 ** 2 - r ** 2 + ax ** 2 - ax0 ** 2 + ay ** 2 - ay0 ** 2)
        try:
            sol, *_ = np.linalg.lstsq(np.asarray(A), np.asarray(b), rcond=None)
        except np.linalg.LinAlgError:  # pragma: no cover
            return None
        return (float(sol[0]), float(sol[1]))
