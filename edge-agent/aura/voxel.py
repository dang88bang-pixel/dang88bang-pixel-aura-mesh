"""Sparse voxel storage: RLE-compressed chunks + a sparse voxel octree.

The reconstructed world is a 3D field of ``(intensity, label)`` pairs. Storing
it densely is hopeless -- a 64 m cube at 5 cm resolution is 2 billion voxels --
but it is overwhelmingly empty, so we use:

* **Chunking**: the world is cut into ``CHUNK_SIZE^3`` blocks addressed by
  integer chunk coordinates. Only touched chunks exist. This mirrors
  ``SpatialChunkEntity`` in the Android Room database.
* **RLE compression**: within a chunk, runs of identical voxels collapse to
  ``(count, intensity, label)`` triples. Empty chunks cost 6 bytes.
* **SVO**: an octree over occupied chunks for fast frustum/radius queries.

The binary chunk format is shared with the Kotlin side, so a CT45P can upload
a chunk blob and the agent can decode it without any conversion:

    magic  'AVX1'   4 bytes
    size   uint8    voxels per edge
    runs   uint32   number of RLE triples
    then `runs` records of:  uint16 count | uint16 intensity | uint8 label
"""

from __future__ import annotations

import math
import struct
import zlib
from dataclasses import dataclass, field
from typing import Iterable, Iterator

import numpy as np

CHUNK_SIZE = 16
MAGIC = b"AVX1"

# semantic labels (kept in sync with the Babylon.js colour table)
LABEL_EMPTY = 0
LABEL_STRUCTURE = 1     # walls, furniture   -> grey
LABEL_PERSON = 2        # living being       -> signal green
LABEL_DEVICE = 3        # BLE token, router  -> amber
LABEL_HAZARD = 4        # danger zone        -> red
LABEL_EXIT = 5          # escape route       -> orange

LABEL_NAMES = {
    LABEL_EMPTY: "empty",
    LABEL_STRUCTURE: "structure",
    LABEL_PERSON: "person",
    LABEL_DEVICE: "device",
    LABEL_HAZARD: "hazard",
    LABEL_EXIT: "exit",
}


@dataclass
class VoxelChunk:
    """A dense ``size^3`` block that knows how to compress itself."""

    cx: int
    cy: int
    cz: int
    size: int = CHUNK_SIZE
    intensity: np.ndarray = field(default=None)  # type: ignore[assignment]
    label: np.ndarray = field(default=None)      # type: ignore[assignment]
    timestamp: float = 0.0

    def __post_init__(self) -> None:
        if self.intensity is None:
            self.intensity = np.zeros((self.size, self.size, self.size), dtype=np.uint16)
        if self.label is None:
            self.label = np.zeros((self.size, self.size, self.size), dtype=np.uint8)

    @property
    def key(self) -> tuple[int, int, int]:
        return (self.cx, self.cy, self.cz)

    @property
    def occupied(self) -> int:
        return int(np.count_nonzero(self.intensity))

    def set(self, lx: int, ly: int, lz: int, intensity: int, label: int = LABEL_STRUCTURE) -> None:
        self.intensity[lz, ly, lx] = np.uint16(max(0, min(65535, int(intensity))))
        self.label[lz, ly, lx] = np.uint8(label)

    def get(self, lx: int, ly: int, lz: int) -> tuple[int, int]:
        return (int(self.intensity[lz, ly, lx]), int(self.label[lz, ly, lx]))

    # -- serialisation -------------------------------------------------
    def to_bytes(self, compress: bool = True) -> bytes:
        """RLE encode, then optionally deflate."""
        flat_i = self.intensity.ravel()
        flat_l = self.label.ravel()
        runs: list[tuple[int, int, int]] = []
        count = 0
        cur_i = int(flat_i[0])
        cur_l = int(flat_l[0])
        for idx in range(len(flat_i)):
            i_val = int(flat_i[idx])
            l_val = int(flat_l[idx])
            if i_val == cur_i and l_val == cur_l and count < 65535:
                count += 1
            else:
                runs.append((count, cur_i, cur_l))
                cur_i, cur_l, count = i_val, l_val, 1
        runs.append((count, cur_i, cur_l))

        payload = bytearray()
        payload += MAGIC
        payload += struct.pack("<BI", self.size, len(runs))
        for run_count, run_i, run_l in runs:
            payload += struct.pack("<HHB", run_count, run_i, run_l)
        blob = bytes(payload)
        return zlib.compress(blob, 6) if compress else blob

    @classmethod
    def from_bytes(cls, cx: int, cy: int, cz: int, blob: bytes, timestamp: float = 0.0) -> "VoxelChunk":
        if not blob.startswith(MAGIC):
            try:
                blob = zlib.decompress(blob)
            except zlib.error as exc:  # pragma: no cover - corrupt input
                raise ValueError(f"not a voxel chunk: {exc}") from exc
        if not blob.startswith(MAGIC):
            raise ValueError("bad chunk magic")
        size, run_count = struct.unpack("<BI", blob[4:9])
        chunk = cls(cx, cy, cz, size=size, timestamp=timestamp)
        flat_i = np.zeros(size ** 3, dtype=np.uint16)
        flat_l = np.zeros(size ** 3, dtype=np.uint8)
        offset = 9
        cursor = 0
        for _ in range(run_count):
            count, intensity, label = struct.unpack("<HHB", blob[offset : offset + 5])
            offset += 5
            end = min(cursor + count, len(flat_i))
            flat_i[cursor:end] = intensity
            flat_l[cursor:end] = label
            cursor = end
        chunk.intensity = flat_i.reshape((size, size, size))
        chunk.label = flat_l.reshape((size, size, size))
        return chunk

    def iter_voxels(self, threshold: int = 0) -> Iterator[tuple[int, int, int, int, int]]:
        """Yield ``(lx, ly, lz, intensity, label)`` for occupied voxels."""
        zs, ys, xs = np.nonzero(self.intensity > threshold)
        for z, y, x in zip(zs, ys, xs):
            yield (int(x), int(y), int(z), int(self.intensity[z, y, x]), int(self.label[z, y, x]))


class VoxelWorld:
    """Chunked sparse voxel field with world<->chunk coordinate mapping."""

    def __init__(self, voxel_size: float = 0.10, chunk_size: int = CHUNK_SIZE) -> None:
        self.voxel_size = float(voxel_size)
        self.chunk_size = int(chunk_size)
        self.chunks: dict[tuple[int, int, int], VoxelChunk] = {}
        self.updates = 0

    # -- coordinates ---------------------------------------------------
    @property
    def chunk_extent(self) -> float:
        return self.voxel_size * self.chunk_size

    def world_to_voxel(self, x: float, y: float, z: float) -> tuple[int, int, int]:
        return (
            int(math.floor(x / self.voxel_size)),
            int(math.floor(y / self.voxel_size)),
            int(math.floor(z / self.voxel_size)),
        )

    def voxel_to_chunk(self, vx: int, vy: int, vz: int) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
        """Split a global voxel index into ``(chunk_key, local_index)``.

        Uses floor division so negative coordinates map correctly -- a plain
        ``int()`` truncation puts ``-1`` and ``0`` in the same chunk and
        silently corrupts everything left/below the origin.
        """
        cx, lx = divmod(vx, self.chunk_size)
        cy, ly = divmod(vy, self.chunk_size)
        cz, lz = divmod(vz, self.chunk_size)
        return ((cx, cy, cz), (lx, ly, lz))

    def chunk_origin(self, key: tuple[int, int, int]) -> tuple[float, float, float]:
        return (
            key[0] * self.chunk_size * self.voxel_size,
            key[1] * self.chunk_size * self.voxel_size,
            key[2] * self.chunk_size * self.voxel_size,
        )

    # -- access --------------------------------------------------------
    def set_voxel(self, x: float, y: float, z: float, intensity: int,
                  label: int = LABEL_STRUCTURE, timestamp: float = 0.0) -> None:
        vx, vy, vz = self.world_to_voxel(x, y, z)
        key, (lx, ly, lz) = self.voxel_to_chunk(vx, vy, vz)
        chunk = self.chunks.get(key)
        if chunk is None:
            chunk = VoxelChunk(*key, size=self.chunk_size)
            self.chunks[key] = chunk
        chunk.set(lx, ly, lz, intensity, label)
        chunk.timestamp = timestamp
        self.updates += 1

    def get_voxel(self, x: float, y: float, z: float) -> tuple[int, int]:
        vx, vy, vz = self.world_to_voxel(x, y, z)
        key, (lx, ly, lz) = self.voxel_to_chunk(vx, vy, vz)
        chunk = self.chunks.get(key)
        return chunk.get(lx, ly, lz) if chunk else (0, LABEL_EMPTY)

    def integrate_points(self, points: Iterable[Iterable[float]], label: int = LABEL_STRUCTURE,
                         intensity: int = 40000, decay: bool = True, timestamp: float = 0.0) -> int:
        """Insert a point cloud; repeated hits reinforce a voxel."""
        count = 0
        for point in points:
            values = list(point)
            if len(values) < 3:
                continue
            x, y, z = float(values[0]), float(values[1]), float(values[2])
            existing, _ = self.get_voxel(x, y, z)
            if decay and existing:
                new_value = int(min(65535, existing + (intensity - existing) * 0.3))
            else:
                new_value = intensity
            self.set_voxel(x, y, z, new_value, label, timestamp)
            count += 1
        return count

    # -- queries -------------------------------------------------------
    def chunks_in_radius(self, center: tuple[float, float, float], radius: float) -> list[VoxelChunk]:
        cx, cy, cz = center
        extent = self.chunk_extent
        out: list[VoxelChunk] = []
        for key, chunk in self.chunks.items():
            ox, oy, oz = self.chunk_origin(key)
            # closest point of the chunk AABB to the query centre
            qx = min(max(cx, ox), ox + extent)
            qy = min(max(cy, oy), oy + extent)
            qz = min(max(cz, oz), oz + extent)
            if math.dist((cx, cy, cz), (qx, qy, qz)) <= radius:
                out.append(chunk)
        return out

    def point_cloud(self, threshold: int = 1000, max_points: int = 20000,
                    labels: set[int] | None = None) -> list[list[float]]:
        """Flatten to ``[x, y, z, normalised_intensity, label]`` records."""
        out: list[list[float]] = []
        for key, chunk in self.chunks.items():
            ox, oy, oz = self.chunk_origin(key)
            for lx, ly, lz, intensity, label in chunk.iter_voxels(threshold):
                if labels is not None and label not in labels:
                    continue
                out.append([
                    round(ox + (lx + 0.5) * self.voxel_size, 3),
                    round(oy + (ly + 0.5) * self.voxel_size, 3),
                    round(oz + (lz + 0.5) * self.voxel_size, 3),
                    round(intensity / 65535.0, 4),
                    label,
                ])
                if len(out) >= max_points:
                    return out
        return out

    def stats(self) -> dict:
        occupied = sum(c.occupied for c in self.chunks.values())
        raw = len(self.chunks) * self.chunk_size ** 3 * 3
        compressed = sum(len(c.to_bytes()) for c in self.chunks.values()) if self.chunks else 0
        by_label: dict[str, int] = {}
        for chunk in self.chunks.values():
            values, counts = np.unique(chunk.label[chunk.intensity > 0], return_counts=True)
            for value, count in zip(values, counts):
                name = LABEL_NAMES.get(int(value), str(int(value)))
                by_label[name] = by_label.get(name, 0) + int(count)
        return {
            "chunks": len(self.chunks),
            "voxel_size": self.voxel_size,
            "chunk_size": self.chunk_size,
            "occupied_voxels": occupied,
            "updates": self.updates,
            "raw_bytes": raw,
            "compressed_bytes": compressed,
            "compression_ratio": round(raw / compressed, 2) if compressed else 0.0,
            "by_label": by_label,
        }

    def prune(self, older_than: float) -> int:
        stale = [k for k, c in self.chunks.items() if c.timestamp and c.timestamp < older_than]
        for key in stale:
            del self.chunks[key]
        return len(stale)


# ----------------------------------------------------------------------
# sparse voxel octree
# ----------------------------------------------------------------------
@dataclass
class OctreeNode:
    """One node of the SVO. Leaves carry a payload, branches have 8 children."""

    center: tuple[float, float, float]
    half_size: float
    depth: int = 0
    children: list["OctreeNode | None"] = field(default_factory=lambda: [None] * 8)
    payload: list[tuple[float, float, float, int, int]] = field(default_factory=list)
    is_leaf: bool = True

    def octant_of(self, x: float, y: float, z: float) -> int:
        index = 0
        if x >= self.center[0]:
            index |= 1
        if y >= self.center[1]:
            index |= 2
        if z >= self.center[2]:
            index |= 4
        return index

    def child_center(self, index: int) -> tuple[float, float, float]:
        quarter = self.half_size / 2.0
        return (
            self.center[0] + (quarter if index & 1 else -quarter),
            self.center[1] + (quarter if index & 2 else -quarter),
            self.center[2] + (quarter if index & 4 else -quarter),
        )

    def contains(self, x: float, y: float, z: float) -> bool:
        return (
            abs(x - self.center[0]) <= self.half_size
            and abs(y - self.center[1]) <= self.half_size
            and abs(z - self.center[2]) <= self.half_size
        )

    def intersects_sphere(self, center: tuple[float, float, float], radius: float) -> bool:
        dx = max(0.0, abs(center[0] - self.center[0]) - self.half_size)
        dy = max(0.0, abs(center[1] - self.center[1]) - self.half_size)
        dz = max(0.0, abs(center[2] - self.center[2]) - self.half_size)
        return dx * dx + dy * dy + dz * dz <= radius * radius


class SparseVoxelOctree:
    """Adaptive octree for fast spatial queries over sparse voxels."""

    def __init__(self, center: tuple[float, float, float] = (0.0, 0.0, 0.0),
                 half_size: float = 64.0, max_depth: int = 10, bucket: int = 16) -> None:
        self.root = OctreeNode(center=center, half_size=half_size)
        self.max_depth = max_depth
        self.bucket = bucket
        self.count = 0

    def insert(self, x: float, y: float, z: float, intensity: int = 40000,
               label: int = LABEL_STRUCTURE) -> bool:
        if not self.root.contains(x, y, z):
            return False
        node = self.root
        while True:
            if node.is_leaf:
                node.payload.append((x, y, z, intensity, label))
                self.count += 1
                if len(node.payload) > self.bucket and node.depth < self.max_depth:
                    self._split(node)
                return True
            index = node.octant_of(x, y, z)
            child = node.children[index]
            if child is None:
                child = OctreeNode(center=node.child_center(index),
                                   half_size=node.half_size / 2.0, depth=node.depth + 1)
                node.children[index] = child
            node = child

    def _split(self, node: OctreeNode) -> None:
        node.is_leaf = False
        payload = node.payload
        node.payload = []
        for x, y, z, intensity, label in payload:
            index = node.octant_of(x, y, z)
            child = node.children[index]
            if child is None:
                child = OctreeNode(center=node.child_center(index),
                                   half_size=node.half_size / 2.0, depth=node.depth + 1)
                node.children[index] = child
            child.payload.append((x, y, z, intensity, label))

    def query_sphere(self, center: tuple[float, float, float], radius: float) -> list[tuple]:
        found: list[tuple] = []
        stack = [self.root]
        r2 = radius * radius
        while stack:
            node = stack.pop()
            if not node.intersects_sphere(center, radius):
                continue
            if node.is_leaf:
                for item in node.payload:
                    dx = item[0] - center[0]
                    dy = item[1] - center[1]
                    dz = item[2] - center[2]
                    if dx * dx + dy * dy + dz * dz <= r2:
                        found.append(item)
            else:
                for child in node.children:
                    if child is not None:
                        stack.append(child)
        return found

    def depth_histogram(self) -> dict[int, int]:
        histogram: dict[int, int] = {}
        stack = [self.root]
        while stack:
            node = stack.pop()
            if node.is_leaf:
                histogram[node.depth] = histogram.get(node.depth, 0) + len(node.payload)
            else:
                for child in node.children:
                    if child is not None:
                        stack.append(child)
        return histogram

    def stats(self) -> dict:
        nodes = 0
        leaves = 0
        stack = [self.root]
        while stack:
            node = stack.pop()
            nodes += 1
            if node.is_leaf:
                leaves += 1
            else:
                for child in node.children:
                    if child is not None:
                        stack.append(child)
        return {"points": self.count, "nodes": nodes, "leaves": leaves, "max_depth": self.max_depth}
