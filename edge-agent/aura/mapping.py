"""Occupancy grid mapping, wall extraction and mesh generation.

The agent maintains a log-odds occupancy grid updated from every LiDAR sweep.
From that grid it derives:

* a decimated **point cloud** for the Three.js viewer,
* **wall segments** (extracted with a split-and-merge / RANSAC-lite pass),
* a watertight-ish **extruded mesh** exportable as glTF, and
* a **navigation grid** consumed by the evacuation solver.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

import numpy as np

LOG_ODDS_HIT = 0.85
LOG_ODDS_MISS = -0.28
LOG_ODDS_MIN = -4.0
LOG_ODDS_MAX = 5.0
OCCUPIED_THRESHOLD = 0.65


@dataclass
class MapStats:
    cells_known: int
    cells_occupied: int
    coverage: float
    updates: int
    area_m2: float

    def as_dict(self) -> dict:
        return {
            "cells_known": self.cells_known,
            "cells_occupied": self.cells_occupied,
            "coverage": round(self.coverage, 4),
            "updates": self.updates,
            "area_m2": round(self.area_m2, 2),
        }


class OccupancyGrid:
    """Log-odds 2D grid with Bresenham ray tracing."""

    def __init__(self, size_m: float = 48.0, resolution: float = 0.10, origin: tuple[float, float] = (-4.0, -4.0)) -> None:
        self.resolution = float(resolution)
        self.origin = np.array(origin, dtype=float)
        self.cells = int(round(size_m / resolution))
        self.grid = np.zeros((self.cells, self.cells), dtype=np.float32)
        self.updates = 0
        self.last_update = 0.0

    # -- coordinate helpers -------------------------------------------
    def world_to_cell(self, x: float, y: float) -> tuple[int, int]:
        cx = int((x - self.origin[0]) / self.resolution)
        cy = int((y - self.origin[1]) / self.resolution)
        return (cx, cy)

    def cell_to_world(self, cx: int, cy: int) -> tuple[float, float]:
        return (
            float(self.origin[0] + (cx + 0.5) * self.resolution),
            float(self.origin[1] + (cy + 0.5) * self.resolution),
        )

    def in_bounds(self, cx: int, cy: int) -> bool:
        return 0 <= cx < self.cells and 0 <= cy < self.cells

    # -- updates -------------------------------------------------------
    def integrate_scan(self, origin: tuple[float, float], points: np.ndarray) -> int:
        """Insert one sweep: free space along each ray, occupied at the hit."""
        if len(points) == 0:
            return 0
        ox, oy = self.world_to_cell(origin[0], origin[1])
        touched = 0
        for px, py in points:
            cx, cy = self.world_to_cell(float(px), float(py))
            if not self.in_bounds(cx, cy):
                continue
            for fx, fy in _bresenham(ox, oy, cx, cy):
                if (fx, fy) == (cx, cy) or not self.in_bounds(fx, fy):
                    continue
                self.grid[fy, fx] = max(LOG_ODDS_MIN, self.grid[fy, fx] + LOG_ODDS_MISS)
            self.grid[cy, cx] = min(LOG_ODDS_MAX, self.grid[cy, cx] + LOG_ODDS_HIT)
            touched += 1
        self.updates += 1
        self.last_update = time.time()
        return touched

    # -- queries -------------------------------------------------------
    def probability(self) -> np.ndarray:
        return 1.0 - 1.0 / (1.0 + np.exp(self.grid))

    def occupied_cells(self) -> np.ndarray:
        prob = self.probability()
        ys, xs = np.where(prob > OCCUPIED_THRESHOLD)
        if len(xs) == 0:
            return np.zeros((0, 3))
        wx = self.origin[0] + (xs + 0.5) * self.resolution
        wy = self.origin[1] + (ys + 0.5) * self.resolution
        return np.column_stack([wx, wy, prob[ys, xs]])

    def point_cloud(self, max_points: int = 6000, height: float = 2.6, layers: int = 3) -> list[list[float]]:
        """Occupied cells extruded into a sparse 3D cloud for the viewer."""
        cells = self.occupied_cells()
        if len(cells) == 0:
            return []
        if len(cells) > max_points // max(1, layers):
            idx = np.linspace(0, len(cells) - 1, max_points // max(1, layers)).astype(int)
            cells = cells[idx]
        out: list[list[float]] = []
        for layer in range(max(1, layers)):
            z = height * (layer + 0.5) / max(1, layers)
            for x, y, p in cells:
                out.append([round(float(x), 3), round(float(y), 3), round(z, 3), round(float(p), 3)])
        return out

    def stats(self) -> MapStats:
        prob = self.probability()
        known = int(np.count_nonzero(np.abs(self.grid) > 0.05))
        occupied = int(np.count_nonzero(prob > OCCUPIED_THRESHOLD))
        total = self.cells * self.cells
        return MapStats(
            cells_known=known,
            cells_occupied=occupied,
            coverage=known / total if total else 0.0,
            updates=self.updates,
            area_m2=known * self.resolution ** 2,
        )

    def navigation_grid(self, inflate: int = 2) -> np.ndarray:
        """Boolean grid: ``True`` = traversable (occupied cells inflated)."""
        blocked = self.probability() > OCCUPIED_THRESHOLD
        if inflate > 0:
            padded = blocked.copy()
            for dy in range(-inflate, inflate + 1):
                for dx in range(-inflate, inflate + 1):
                    if dx == 0 and dy == 0:
                        continue
                    padded |= np.roll(np.roll(blocked, dy, axis=0), dx, axis=1)
            blocked = padded
        return ~blocked

    def to_dict(self, max_points: int = 4000) -> dict:
        return {
            "resolution": self.resolution,
            "origin": [float(self.origin[0]), float(self.origin[1])],
            "cells": self.cells,
            "points": self.point_cloud(max_points=max_points),
            "stats": self.stats().as_dict(),
        }

    def snapshot_bytes(self) -> bytes:
        return self.grid.astype(np.float32).tobytes()

    def load_bytes(self, blob: bytes) -> None:
        arr = np.frombuffer(blob, dtype=np.float32)
        if arr.size == self.cells * self.cells:
            self.grid = arr.reshape(self.cells, self.cells).copy()


def _bresenham(x0: int, y0: int, x1: int, y1: int) -> list[tuple[int, int]]:
    """Integer line rasterisation (free-space carving)."""
    points: list[tuple[int, int]] = []
    dx = abs(x1 - x0)
    dy = -abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx + dy
    x, y = x0, y0
    for _ in range(4096):
        points.append((x, y))
        if x == x1 and y == y1:
            break
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x += sx
        if e2 <= dx:
            err += dx
            y += sy
    return points


# ----------------------------------------------------------------------
# wall extraction
# ----------------------------------------------------------------------
@dataclass
class Segment:
    x1: float
    y1: float
    x2: float
    y2: float
    support: int = 0

    @property
    def length(self) -> float:
        return math.hypot(self.x2 - self.x1, self.y2 - self.y1)

    def as_dict(self) -> dict:
        return {
            "x1": round(self.x1, 3),
            "y1": round(self.y1, 3),
            "x2": round(self.x2, 3),
            "y2": round(self.y2, 3),
            "length": round(self.length, 3),
            "support": self.support,
        }


def extract_walls(points: np.ndarray, min_support: int = 14, tolerance: float = 0.09,
                  max_segments: int = 48) -> list[Segment]:
    """RANSAC-lite line extraction over occupied cells -> wall segments."""
    if len(points) < min_support:
        return []
    xy = np.asarray(points, dtype=float)[:, :2]
    remaining = xy.copy()
    rng = np.random.default_rng(97)
    segments: list[Segment] = []

    for _ in range(max_segments):
        if len(remaining) < min_support:
            break
        best_inliers: np.ndarray | None = None
        best_dir = None
        best_pt = None
        for _ in range(90):
            i, j = rng.integers(0, len(remaining), 2)
            if i == j:
                continue
            p0, p1 = remaining[i], remaining[j]
            d = p1 - p0
            norm = float(np.linalg.norm(d))
            if norm < 0.35:
                continue
            d = d / norm
            n = np.array([-d[1], d[0]])
            dist = np.abs((remaining - p0) @ n)
            inliers = dist < tolerance
            count = int(np.count_nonzero(inliers))
            if best_inliers is None or count > int(np.count_nonzero(best_inliers)):
                best_inliers, best_dir, best_pt = inliers, d, p0
        if best_inliers is None or int(np.count_nonzero(best_inliers)) < min_support:
            break
        pts = remaining[best_inliers]
        # total least squares refit
        centroid = pts.mean(axis=0)
        centred = pts - centroid
        _u, _s, vt = np.linalg.svd(centred, full_matrices=False)
        direction = vt[0]
        t = centred @ direction
        p_start = centroid + direction * float(t.min())
        p_end = centroid + direction * float(t.max())
        seg = Segment(float(p_start[0]), float(p_start[1]), float(p_end[0]), float(p_end[1]),
                      int(np.count_nonzero(best_inliers)))
        if seg.length >= 0.45:
            segments.append(seg)
        remaining = remaining[~best_inliers]

    segments.sort(key=lambda s: s.length, reverse=True)
    return segments


# ----------------------------------------------------------------------
# mesh generation (extruded walls -> glTF-ready triangle soup)
# ----------------------------------------------------------------------
def build_mesh(segments: list[Segment], height: float = 2.7, thickness: float = 0.12,
               floor_bounds: tuple[float, float, float, float] | None = None) -> dict:
    """Extrude wall segments into boxes; returns positions/normals/indices."""
    positions: list[float] = []
    normals: list[float] = []
    indices: list[int] = []

    def add_quad(a, b, c, d) -> None:
        base = len(positions) // 3
        u = np.subtract(b, a)
        v = np.subtract(d, a)
        n = np.cross(u, v)
        norm = float(np.linalg.norm(n))
        n = (n / norm) if norm > 1e-9 else np.array([0.0, 0.0, 1.0])
        for vertex in (a, b, c, d):
            positions.extend([float(vertex[0]), float(vertex[1]), float(vertex[2])])
            normals.extend([float(n[0]), float(n[1]), float(n[2])])
        indices.extend([base, base + 1, base + 2, base, base + 2, base + 3])

    for seg in segments:
        dx, dy = seg.x2 - seg.x1, seg.y2 - seg.y1
        length = math.hypot(dx, dy)
        if length < 1e-6:
            continue
        ux, uy = dx / length, dy / length
        nx, ny = -uy * thickness / 2.0, ux * thickness / 2.0
        p1 = (seg.x1 + nx, seg.y1 + ny)
        p2 = (seg.x2 + nx, seg.y2 + ny)
        p3 = (seg.x2 - nx, seg.y2 - ny)
        p4 = (seg.x1 - nx, seg.y1 - ny)
        bottom = [(p[0], p[1], 0.0) for p in (p1, p2, p3, p4)]
        top = [(p[0], p[1], height) for p in (p1, p2, p3, p4)]
        add_quad(top[0], top[1], top[2], top[3])                    # roof
        add_quad(bottom[3], bottom[2], bottom[1], bottom[0])        # floor of the box
        for i in range(4):
            j = (i + 1) % 4
            add_quad(bottom[i], bottom[j], top[j], top[i])          # side

    if floor_bounds is not None:
        x0, y0, x1, y1 = floor_bounds
        add_quad((x0, y0, 0.0), (x1, y0, 0.0), (x1, y1, 0.0), (x0, y1, 0.0))

    return {
        "positions": positions,
        "normals": normals,
        "indices": indices,
        "vertex_count": len(positions) // 3,
        "triangle_count": len(indices) // 3,
    }


def mesh_to_gltf(mesh: dict, name: str = "AuraScan") -> dict:
    """Pack a mesh dict into a self-contained glTF 2.0 document (base64 buffer)."""
    import base64
    import struct

    positions = mesh["positions"]
    normals = mesh["normals"]
    indices = mesh["indices"]
    if not positions or not indices:
        positions = [0.0, 0.0, 0.0]
        normals = [0.0, 0.0, 1.0]
        indices = [0, 0, 0]

    pos_bytes = struct.pack(f"<{len(positions)}f", *positions)
    nrm_bytes = struct.pack(f"<{len(normals)}f", *normals)
    idx_bytes = struct.pack(f"<{len(indices)}I", *indices)
    pad = lambda b: b + b"\x00" * ((4 - len(b) % 4) % 4)  # noqa: E731
    pos_bytes, nrm_bytes = pad(pos_bytes), pad(nrm_bytes)
    buffer = pos_bytes + nrm_bytes + idx_bytes

    px = positions[0::3] or [0.0]
    py = positions[1::3] or [0.0]
    pz = positions[2::3] or [0.0]

    return {
        "asset": {"version": "2.0", "generator": "Aura Edge Agent"},
        "scene": 0,
        "scenes": [{"name": name, "nodes": [0]}],
        "nodes": [{"name": name, "mesh": 0}],
        "meshes": [
            {
                "name": name,
                "primitives": [
                    {"attributes": {"POSITION": 0, "NORMAL": 1}, "indices": 2, "material": 0, "mode": 4}
                ],
            }
        ],
        "materials": [
            {
                "name": "AuraWall",
                "pbrMetallicRoughness": {
                    "baseColorFactor": [0.62, 0.68, 0.78, 1.0],
                    "metallicFactor": 0.05,
                    "roughnessFactor": 0.85,
                },
                "doubleSided": True,
            }
        ],
        "buffers": [{"byteLength": len(buffer), "uri": "data:application/octet-stream;base64," + base64.b64encode(buffer).decode()}],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": len(pos_bytes), "target": 34962},
            {"buffer": 0, "byteOffset": len(pos_bytes), "byteLength": len(nrm_bytes), "target": 34962},
            {"buffer": 0, "byteOffset": len(pos_bytes) + len(nrm_bytes), "byteLength": len(idx_bytes), "target": 34963},
        ],
        "accessors": [
            {
                "bufferView": 0,
                "componentType": 5126,
                "count": len(positions) // 3,
                "type": "VEC3",
                "min": [min(px), min(py), min(pz)],
                "max": [max(px), max(py), max(pz)],
            },
            {"bufferView": 1, "componentType": 5126, "count": len(normals) // 3, "type": "VEC3"},
            {"bufferView": 2, "componentType": 5125, "count": len(indices), "type": "SCALAR"},
        ],
    }
