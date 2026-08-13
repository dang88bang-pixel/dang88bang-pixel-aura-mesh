"""Common plumbing shared by all sensor drivers."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

LOGGER = logging.getLogger("aura.sensors")

try:  # pragma: no cover - pyserial is optional at runtime
    import serial  # type: ignore

    HAVE_SERIAL = True
except Exception:  # pragma: no cover
    serial = None  # type: ignore
    HAVE_SERIAL = False


@dataclass
class SensorStatus:
    """Health of a single driver, surfaced through the REST API."""

    name: str
    connected: bool = False
    simulated: bool = True
    samples: int = 0
    errors: int = 0
    last_sample_ts: float = 0.0
    detail: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        age = time.time() - self.last_sample_ts if self.last_sample_ts else None
        return {
            "name": self.name,
            "connected": self.connected,
            "simulated": self.simulated,
            "samples": self.samples,
            "errors": self.errors,
            "age_seconds": round(age, 3) if age is not None else None,
            "healthy": self.connected and (age is None or age < 5.0),
            "detail": self.detail,
            **self.extra,
        }


class SensorDriver:
    """Base class: owns a :class:`SensorStatus` and an optional serial port."""

    name = "sensor"

    def __init__(self, port: str = "", baud: int = 115200, simulate: bool = True) -> None:
        self.port = port
        self.baud = baud
        self.simulate = simulate or not port or not HAVE_SERIAL
        self.status = SensorStatus(name=self.name, simulated=self.simulate)
        self._serial: Optional[Any] = None
        self._t0 = time.time()

    # -- lifecycle -----------------------------------------------------
    def open(self) -> bool:
        if self.simulate:
            self.status.connected = True
            self.status.simulated = True
            self.status.detail = "synthetic driver"
            return True
        try:  # pragma: no cover - requires hardware
            self._serial = serial.Serial(self.port, self.baud, timeout=0.2)  # type: ignore[union-attr]
            self.status.connected = True
            self.status.simulated = False
            self.status.detail = f"{self.port}@{self.baud}"
            return True
        except Exception as exc:  # pragma: no cover
            LOGGER.warning("%s: cannot open %s (%s) - falling back to simulation", self.name, self.port, exc)
            self.simulate = True
            self.status.simulated = True
            self.status.connected = True
            self.status.errors += 1
            self.status.detail = f"fallback: {exc}"
            return False

    def close(self) -> None:
        if self._serial is not None:  # pragma: no cover - hardware only
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None
        self.status.connected = False

    # -- helpers -------------------------------------------------------
    def _write(self, payload: bytes) -> None:  # pragma: no cover - hardware only
        if self._serial is not None:
            try:
                self._serial.write(payload)
                self._serial.flush()
            except Exception as exc:
                self.status.errors += 1
                LOGGER.debug("%s: write failed (%s)", self.name, exc)

    def _read(self, size: int) -> bytes:  # pragma: no cover - hardware only
        if self._serial is None:
            return b""
        try:
            return self._serial.read(size)
        except Exception as exc:
            self.status.errors += 1
            LOGGER.debug("%s: read failed (%s)", self.name, exc)
            return b""

    def _mark(self, detail: str = "") -> None:
        self.status.samples += 1
        self.status.last_sample_ts = time.time()
        if detail:
            self.status.detail = detail

    @property
    def elapsed(self) -> float:
        return time.time() - self._t0

    @property
    def info(self) -> dict:
        return self.status.as_dict()

    def read(self):  # pragma: no cover - interface
        raise NotImplementedError
