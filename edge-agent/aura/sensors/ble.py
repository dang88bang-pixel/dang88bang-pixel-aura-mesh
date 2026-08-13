"""BLE token scanner: BlueZ/`bleak` when available, otherwise a path-loss model."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

import numpy as np

from ..world import DEFAULT_BLE_TOKENS, WORLD, World
from .base import SensorDriver


@dataclass
class BleBeacon:
    """A single advertisement observation."""

    address: str
    rssi: int
    name: str = ""
    timestamp: float = 0.0
    tx_power: int = -59

    @property
    def distance(self) -> float:
        """Log-distance path-loss estimate (n = 2.4 for indoor office)."""
        return float(10 ** ((self.tx_power - self.rssi) / (10.0 * 2.4)))

    def as_dict(self) -> dict:
        return {
            "address": self.address,
            "rssi": self.rssi,
            "name": self.name,
            "timestamp": self.timestamp,
            "distance": round(self.distance, 2),
        }


class BleDriver(SensorDriver):
    """Passive scanner producing an address -> RSSI map."""

    name = "ble"

    def __init__(
        self,
        adapter: str = "hci0",
        simulate: bool = True,
        world: World | None = None,
        tokens: dict[str, tuple[str, float, float]] | None = None,
    ) -> None:
        super().__init__("", 0, simulate)
        self.adapter = adapter
        self.world = world or WORLD
        self.tokens = dict(tokens or DEFAULT_BLE_TOKENS)
        self._rng = np.random.default_rng(31)
        self._pose = (2.0, 2.0, 0.0)
        self._smoothed: dict[str, float] = {}
        self._backend = None

    def open(self) -> bool:
        if self.simulate:
            self.status.connected = True
            self.status.detail = f"synthetic ({len(self.tokens)} tokens)"
            self.status.extra["tokens"] = list(self.tokens)
            return True
        try:  # pragma: no cover - hardware only
            from bleak import BleakScanner  # type: ignore

            self._backend = BleakScanner
            self.status.connected = True
            self.status.simulated = False
            self.status.detail = f"bleak/{self.adapter}"
            return True
        except Exception as exc:  # pragma: no cover
            self.simulate = True
            self.status.simulated = True
            self.status.connected = True
            self.status.detail = f"fallback: {exc}"
            return False

    def set_pose(self, x: float, y: float, yaw: float) -> None:
        self._pose = (float(x), float(y), float(yaw))

    def add_token(self, address: str, label: str, x: float, y: float) -> None:
        self.tokens[address] = (label, x, y)
        self.status.extra["tokens"] = list(self.tokens)

    def remove_token(self, address: str) -> bool:
        return self.tokens.pop(address, None) is not None

    # ------------------------------------------------------------------
    def read(self) -> list[BleBeacon]:
        if self.simulate:
            return self._simulate()
        return self._read_hardware()  # pragma: no cover - hardware only

    def _simulate(self) -> list[BleBeacon]:
        px, py, _ = self._pose
        now = time.time()
        out: list[BleBeacon] = []
        for address, (label, tx, ty) in self.tokens.items():
            dist = max(0.35, math.hypot(tx - px, ty - py))
            walls = self.world.wall_count_between((px, py), (tx, ty))
            # log-distance path loss + 4.5 dB per wall + shadow fading
            rssi = -59.0 - 10 * 2.4 * math.log10(dist) - 4.5 * walls
            rssi += float(self._rng.normal(0.0, 2.2))
            prev = self._smoothed.get(address, rssi)
            smooth = 0.65 * prev + 0.35 * rssi   # exponential moving average
            self._smoothed[address] = smooth
            if smooth < -99:
                continue
            out.append(BleBeacon(address, int(round(smooth)), label, now))
        self._mark()
        return out

    def _read_hardware(self) -> list[BleBeacon]:  # pragma: no cover - hardware only
        import asyncio

        async def _scan() -> list[BleBeacon]:
            devices = await self._backend.discover(timeout=1.0, return_adv=True)  # type: ignore[union-attr]
            found: list[BleBeacon] = []
            now = time.time()
            for address, (device, adv) in devices.items():
                found.append(
                    BleBeacon(
                        address=address,
                        rssi=int(adv.rssi),
                        name=device.name or adv.local_name or "",
                        timestamp=now,
                        tx_power=int(adv.tx_power) if adv.tx_power is not None else -59,
                    )
                )
            return found

        try:
            beacons = asyncio.get_event_loop().run_until_complete(_scan())
        except Exception as exc:
            self.status.errors += 1
            self.status.detail = f"scan error: {exc}"
            return []
        if beacons:
            self._mark()
        return beacons

    # ------------------------------------------------------------------
    def multilaterate(self, beacons: list[BleBeacon]) -> tuple[float, float, float] | None:
        """Weighted least-squares position from >= 3 known tokens.

        Returns ``(x, y, sigma)``; sigma grows with RSSI residual spread.
        """
        known = [(b, self.tokens[b.address]) for b in beacons if b.address in self.tokens]
        if len(known) < 3:
            return None
        b0, (_l0, x0, y0) = known[0]
        r0 = b0.distance
        A, rhs, weights = [], [], []
        for beacon, (_label, x, y) in known[1:]:
            r = beacon.distance
            A.append([2.0 * (x - x0), 2.0 * (y - y0)])
            rhs.append(r0 ** 2 - r ** 2 + x ** 2 - x0 ** 2 + y ** 2 - y0 ** 2)
            weights.append(1.0 / max(0.5, r))
        A_arr = np.asarray(A)
        w = np.diag(weights)
        try:
            sol, residuals, *_ = np.linalg.lstsq(w @ A_arr, w @ np.asarray(rhs), rcond=None)
        except np.linalg.LinAlgError:  # pragma: no cover
            return None
        est = (float(sol[0]), float(sol[1]))
        errs = []
        for beacon, (_label, x, y) in known:
            errs.append(abs(math.hypot(est[0] - x, est[1] - y) - beacon.distance))
        sigma = float(max(0.8, np.mean(errs) if errs else 2.0))
        return (est[0], est[1], sigma)
