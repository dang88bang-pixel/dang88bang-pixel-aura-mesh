"""Synthetic building used by the simulation drivers.

The virtual world is a small office floor: an outer shell plus interior
partition walls and doorways. LiDAR/mmWave simulators ray-cast against these
segments, the evacuation solver path-plans through the same geometry and the
web visualiser draws the ground-truth outline, so every subsystem agrees on
one single source of truth.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence

import numpy as np


@dataclass(frozen=True)
class Wall:
    """Axis-agnostic 2D wall segment with a height, in metres."""

    x1: float
    y1: float
    x2: float
    y2: float
    height: float = 2.7
    kind: str = "wall"

    def as_dict(self) -> dict:
        return {
            "x1": self.x1,
            "y1": self.y1,
            "x2": self.x2,
            "y2": self.y2,
            "height": self.height,
            "kind": self.kind,
        }

    @property
    def length(self) -> float:
        return math.hypot(self.x2 - self.x1, self.y2 - self.y1)


@dataclass(frozen=True)
class Exit:
    """Evacuation target."""

    name: str
    x: float
    y: float

    def as_dict(self) -> dict:
        return {"name": self.name, "x": self.x, "y": self.y}


def _room(x0: float, y0: float, x1: float, y1: float, *, doors: Sequence[tuple[str, float, float]] = ()) -> list[Wall]:
    """Rectangular room with optional door gaps.

    ``doors`` entries are ``(side, start, end)`` where side is one of
    ``n/s/e/w`` and start/end are absolute coordinates along that side.
    """
    segments: list[Wall] = []

    def split(side: str, a: tuple[float, float], b: tuple[float, float]) -> None:
        gaps = [(s, e) for (sd, s, e) in doors if sd == side]
        horizontal = abs(a[1] - b[1]) < 1e-9
        if not gaps:
            segments.append(Wall(a[0], a[1], b[0], b[1]))
            return
        if horizontal:
            xs = sorted([a[0], b[0]])
            cursor = xs[0]
            for gs, ge in sorted(gaps):
                if gs > cursor:
                    segments.append(Wall(cursor, a[1], gs, a[1]))
                cursor = max(cursor, ge)
            if cursor < xs[1]:
                segments.append(Wall(cursor, a[1], xs[1], a[1]))
        else:
            ys = sorted([a[1], b[1]])
            cursor = ys[0]
            for gs, ge in sorted(gaps):
                if gs > cursor:
                    segments.append(Wall(a[0], cursor, a[0], gs))
                cursor = max(cursor, ge)
            if cursor < ys[1]:
                segments.append(Wall(a[0], cursor, a[0], ys[1]))

    split("s", (x0, y0), (x1, y0))
    split("n", (x0, y1), (x1, y1))
    split("w", (x0, y0), (x0, y1))
    split("e", (x1, y0), (x1, y1))
    return segments


def default_floorplan() -> list[Wall]:
    """A ~20 x 14 m office floor with four rooms and a corridor."""
    walls: list[Wall] = []
    # outer shell (main entrance gap on the south wall)
    walls += _room(0, 0, 20, 14, doors=(("s", 9.0, 11.0), ("e", 6.0, 8.0)))
    # north-west office
    walls += _room(0.2, 8.0, 7.0, 13.8, doors=(("s", 3.0, 4.2),))
    # north-east lab
    walls += _room(12.0, 8.0, 19.8, 13.8, doors=(("s", 15.0, 16.2),))
    # south-west storage
    walls += _room(0.2, 0.2, 5.5, 5.5, doors=(("e", 2.0, 3.2),))
    # server closet (thick internal core, no door on the north side)
    walls += _room(13.5, 2.0, 17.5, 5.5, doors=(("w", 3.0, 4.2),))
    return walls


DEFAULT_EXITS = [
    Exit("Haupteingang", 10.0, -0.4),
    Exit("Notausgang Ost", 20.4, 7.0),
]

DEFAULT_BLE_TOKENS = {
    "AA:BB:CC:00:01": ("Token-Flur", 10.0, 6.5),
    "AA:BB:CC:00:02": ("Token-Buero", 3.5, 11.0),
    "AA:BB:CC:00:03": ("Token-Labor", 16.0, 11.0),
    "AA:BB:CC:00:04": ("Token-Lager", 2.8, 2.8),
}

DEFAULT_UWB_ANCHORS = {
    "ANCHOR-A": (0.3, 0.3, 2.4),
    "ANCHOR-B": (19.7, 0.3, 2.4),
    "ANCHOR-C": (19.7, 13.7, 2.4),
    "ANCHOR-D": (0.3, 13.7, 2.4),
}


class World:
    """Ray-castable representation of the synthetic building."""

    def __init__(self, walls: Iterable[Wall] | None = None) -> None:
        self.walls: list[Wall] = list(walls) if walls is not None else default_floorplan()
        self.exits = list(DEFAULT_EXITS)
        self._segments = np.array(
            [[w.x1, w.y1, w.x2, w.y2] for w in self.walls], dtype=float
        ) if self.walls else np.zeros((0, 4))

    # ------------------------------------------------------------------
    def bounds(self) -> tuple[float, float, float, float]:
        if not self.walls:
            return (0.0, 0.0, 1.0, 1.0)
        xs = self._segments[:, [0, 2]]
        ys = self._segments[:, [1, 3]]
        return (float(xs.min()), float(ys.min()), float(xs.max()), float(ys.max()))

    def raycast(self, origin: Sequence[float], angle: float, max_range: float = 25.0) -> float:
        """Distance from ``origin`` along ``angle`` to the nearest wall."""
        if len(self._segments) == 0:
            return max_range
        ox, oy = float(origin[0]), float(origin[1])
        dx, dy = math.cos(angle), math.sin(angle)

        x1 = self._segments[:, 0]
        y1 = self._segments[:, 1]
        x2 = self._segments[:, 2]
        y2 = self._segments[:, 3]
        sx = x2 - x1
        sy = y2 - y1

        denom = dx * sy - dy * sx
        safe = np.where(np.abs(denom) < 1e-12, np.nan, denom)
        t = ((x1 - ox) * sy - (y1 - oy) * sx) / safe   # along the ray
        u = ((x1 - ox) * dy - (y1 - oy) * dx) / safe   # along the segment
        valid = (t > 1e-3) & (t < max_range) & (u >= 0.0) & (u <= 1.0)
        if not np.any(valid):
            return max_range
        return float(np.nanmin(np.where(valid, t, np.inf)))

    def scan(self, origin: Sequence[float], yaw: float, beams: int = 360, max_range: float = 16.0) -> np.ndarray:
        """Full 360 deg LiDAR sweep -> array of ``(angle_body, distance)``."""
        angles = np.linspace(-math.pi, math.pi, beams, endpoint=False)
        out = np.empty((beams, 2), dtype=float)
        for i, a in enumerate(angles):
            out[i, 0] = a
            out[i, 1] = self.raycast(origin, yaw + a, max_range)
        return out

    def is_free(self, x: float, y: float, clearance: float = 0.25) -> bool:
        """True when a point is at least ``clearance`` away from every wall."""
        if len(self._segments) == 0:
            return True
        px, py = float(x), float(y)
        x1 = self._segments[:, 0]
        y1 = self._segments[:, 1]
        vx = self._segments[:, 2] - x1
        vy = self._segments[:, 3] - y1
        seg_len2 = vx * vx + vy * vy
        seg_len2 = np.where(seg_len2 < 1e-12, 1e-12, seg_len2)
        t = np.clip(((px - x1) * vx + (py - y1) * vy) / seg_len2, 0.0, 1.0)
        cx = x1 + t * vx
        cy = y1 + t * vy
        dist = np.hypot(px - cx, py - cy)
        return bool(dist.min() > clearance)

    def visible(self, a: Sequence[float], b: Sequence[float]) -> bool:
        """Line-of-sight test (used for UWB through-wall attenuation)."""
        ax, ay = float(a[0]), float(a[1])
        bx, by = float(b[0]), float(b[1])
        dist = math.hypot(bx - ax, by - ay)
        if dist < 1e-6:
            return True
        angle = math.atan2(by - ay, bx - ax)
        return self.raycast((ax, ay), angle, dist + 0.01) >= dist - 0.02

    def wall_count_between(self, a: Sequence[float], b: Sequence[float]) -> int:
        """Number of wall crossings along a segment (RF attenuation model)."""
        if len(self._segments) == 0:
            return 0
        ax, ay = float(a[0]), float(a[1])
        bx, by = float(b[0]), float(b[1])
        rx, ry = bx - ax, by - ay
        x1 = self._segments[:, 0]
        y1 = self._segments[:, 1]
        sx = self._segments[:, 2] - x1
        sy = self._segments[:, 3] - y1
        denom = rx * sy - ry * sx
        safe = np.where(np.abs(denom) < 1e-12, np.nan, denom)
        t = ((x1 - ax) * sy - (y1 - ay) * sx) / safe
        u = ((x1 - ax) * ry - (y1 - ay) * rx) / safe
        hits = (t > 0.0) & (t < 1.0) & (u >= 0.0) & (u <= 1.0)
        return int(np.count_nonzero(hits))

    def random_free_point(self, rng: np.random.Generator, clearance: float = 0.4) -> tuple[float, float]:
        x0, y0, x1, y1 = self.bounds()
        for _ in range(256):
            x = float(rng.uniform(x0 + 0.5, x1 - 0.5))
            y = float(rng.uniform(y0 + 0.5, y1 - 0.5))
            if self.is_free(x, y, clearance):
                return (x, y)
        return ((x0 + x1) / 2.0, (y0 + y1) / 2.0)

    def as_dict(self) -> dict:
        x0, y0, x1, y1 = self.bounds()
        return {
            "walls": [w.as_dict() for w in self.walls],
            "exits": [e.as_dict() for e in self.exits],
            "bounds": {"min_x": x0, "min_y": y0, "max_x": x1, "max_y": y1},
            "anchors": {k: list(v) for k, v in DEFAULT_UWB_ANCHORS.items()},
            "tokens": {k: {"label": v[0], "x": v[1], "y": v[2]} for k, v in DEFAULT_BLE_TOKENS.items()},
        }


WORLD = World()
