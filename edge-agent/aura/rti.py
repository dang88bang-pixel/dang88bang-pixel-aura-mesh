"""Radio Tomographic Imaging (RTI) with compressed sensing.

RTI reconstructs an attenuation field from the RSSI drop measured on every
link of a radio mesh. With ``K`` nodes there are ``K*(K-1)/2`` links; each
link ``l`` that crosses voxel ``j`` contributes a weight ``W[l, j]``, giving
the linear forward model::

    y = W x + n

``y`` is the vector of per-link RSSI *variance* (or mean shadowing loss),
``x`` the attenuation image we want, and ``n`` the noise. Because ``x`` is
sparse -- only a handful of voxels actually contain a person -- an L1 prior
recovers far more detail than plain Tikhonov, which is the whole point of
compressed sensing.

This module is the **reference implementation**. ``android-app/app/src/main/
cpp/voxel_fusion.cpp`` is a line-by-line C++/Eigen port of
:func:`reconstruct_l1_fista`, and ``tests/test_rti.py`` pins down the numeric
behaviour both must reproduce.

Accuracy note
-------------
Published handheld/sparse-array RTI systems resolve a person to roughly
**1-2 m**, not the sub-0.5 m sometimes quoted for dense pre-installed
arrays (30+ nodes ringing the room). See ``docs/performance_targets.md``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

# ----------------------------------------------------------------------
# geometry / forward model
# ----------------------------------------------------------------------


@dataclass
class RtiGrid:
    """Regular 2D voxel grid covering the monitored area."""

    min_x: float
    min_y: float
    max_x: float
    max_y: float
    resolution: float = 0.25

    def __post_init__(self) -> None:
        self.nx = max(1, int(round((self.max_x - self.min_x) / self.resolution)))
        self.ny = max(1, int(round((self.max_y - self.min_y) / self.resolution)))

    @property
    def voxel_count(self) -> int:
        return self.nx * self.ny

    def centers(self) -> np.ndarray:
        """``(N, 2)`` array of voxel centre coordinates, row-major."""
        xs = self.min_x + (np.arange(self.nx) + 0.5) * self.resolution
        ys = self.min_y + (np.arange(self.ny) + 0.5) * self.resolution
        gx, gy = np.meshgrid(xs, ys, indexing="xy")
        return np.column_stack([gx.ravel(), gy.ravel()])

    def index_of(self, x: float, y: float) -> int | None:
        i = int((x - self.min_x) / self.resolution)
        j = int((y - self.min_y) / self.resolution)
        if not (0 <= i < self.nx and 0 <= j < self.ny):
            return None
        return j * self.nx + i

    def reshape(self, image: np.ndarray) -> np.ndarray:
        return np.asarray(image).reshape(self.ny, self.nx)

    def as_dict(self) -> dict:
        return {
            "min_x": self.min_x,
            "min_y": self.min_y,
            "max_x": self.max_x,
            "max_y": self.max_y,
            "resolution": self.resolution,
            "nx": self.nx,
            "ny": self.ny,
        }


def build_weight_matrix(
    grid: RtiGrid,
    nodes: np.ndarray,
    links: list[tuple[int, int]] | None = None,
    ellipse_width: float = 0.35,
    model: str = "ellipse",
) -> tuple[np.ndarray, list[tuple[int, int]]]:
    """Build the RTI forward operator ``W``.

    ``model="ellipse"`` uses the classic Wilson & Patwari weighting: a voxel
    influences a link when it lies inside the first Fresnel-like ellipse with
    foci at the two nodes, normalised by ``1/sqrt(link_length)``.
    ``model="line"`` is the cheaper straight-line (Radon) approximation.
    """
    nodes = np.asarray(nodes, dtype=float)
    if links is None:
        links = [(i, j) for i in range(len(nodes)) for j in range(i + 1, len(nodes))]
    centers = grid.centers()
    W = np.zeros((len(links), grid.voxel_count), dtype=float)

    for row, (a, b) in enumerate(links):
        pa, pb = nodes[a][:2], nodes[b][:2]
        link_length = float(np.linalg.norm(pb - pa))
        if link_length < 1e-6:
            continue
        da = np.linalg.norm(centers - pa, axis=1)
        db = np.linalg.norm(centers - pb, axis=1)
        excess = da + db - link_length          # 0 exactly on the line
        if model == "line":
            weight = (excess < grid.resolution).astype(float)
        else:
            weight = (excess < ellipse_width).astype(float)
        W[row] = weight / math.sqrt(link_length)
    return W, links


def simulate_measurements(
    W: np.ndarray,
    truth: np.ndarray,
    noise_sigma: float = 0.0,
    rng: np.random.Generator | None = None,
) -> np.ndarray:
    """Forward-project a ground-truth image into link measurements."""
    y = W @ np.asarray(truth, dtype=float).ravel()
    if noise_sigma > 0:
        rng = rng or np.random.default_rng(0)
        y = y + rng.normal(0.0, noise_sigma, size=y.shape)
    return y


# ----------------------------------------------------------------------
# reconstruction
# ----------------------------------------------------------------------
def reconstruct_tikhonov(W: np.ndarray, y: np.ndarray, alpha: float = 0.1) -> np.ndarray:
    """Regularised least squares: ``min ||Wx - y||^2 + alpha*||x||^2``.

    Closed form, fast, but smears a point target across many voxels.
    """
    W = np.asarray(W, dtype=float)
    y = np.asarray(y, dtype=float).ravel()
    n = W.shape[1]
    A = W.T @ W + alpha * np.eye(n)
    b = W.T @ y
    try:
        return np.linalg.solve(A, b)
    except np.linalg.LinAlgError:  # pragma: no cover - singular system
        return np.linalg.lstsq(A, b, rcond=None)[0]


def soft_threshold(x: np.ndarray, threshold: float) -> np.ndarray:
    """L1 proximal operator: ``sign(x) * max(|x| - t, 0)``."""
    return np.sign(x) * np.maximum(np.abs(x) - threshold, 0.0)


def power_iteration_lipschitz(W: np.ndarray, iterations: int = 60) -> float:
    """Largest eigenvalue of ``W^T W`` -- the Lipschitz constant of the gradient.

    Using ``||W||_F^2`` (as naive implementations do) massively over-estimates
    this for wide matrices, which makes FISTA take tiny steps and appear not
    to converge. Power iteration gives the correct spectral norm.
    """
    W = np.asarray(W, dtype=float)
    if W.size == 0:
        return 1.0
    rng = np.random.default_rng(0)
    v = rng.normal(size=W.shape[1])
    norm = np.linalg.norm(v)
    if norm < 1e-12:
        return 1.0
    v /= norm
    eigenvalue = 1.0
    for _ in range(iterations):
        w = W.T @ (W @ v)
        norm = float(np.linalg.norm(w))
        if norm < 1e-18:
            return 1.0
        v = w / norm
        eigenvalue = norm
    return max(eigenvalue, 1e-9)


@dataclass
class FistaResult:
    """Reconstruction plus convergence diagnostics."""

    image: np.ndarray
    iterations: int
    objective: list[float] = field(default_factory=list)
    converged: bool = False
    lipschitz: float = 0.0

    @property
    def sparsity(self) -> float:
        total = self.image.size
        return float(np.count_nonzero(np.abs(self.image) > 1e-9) / total) if total else 0.0


def reconstruct_l1_fista(
    W: np.ndarray,
    y: np.ndarray,
    alpha: float = 0.1,
    max_iter: int = 200,
    tolerance: float = 1e-6,
    non_negative: bool = True,
    lipschitz: float | None = None,
) -> FistaResult:
    """Compressed sensing via FISTA (Beck & Teboulle 2009).

    Solves ``min_x 0.5*||Wx - y||^2 + alpha*||x||_1`` with Nesterov
    acceleration, giving O(1/k^2) convergence instead of ISTA's O(1/k).

    ``non_negative`` projects onto ``x >= 0``: RF attenuation cannot be
    negative, and the constraint measurably sharpens the reconstruction.
    """
    W = np.asarray(W, dtype=float)
    y = np.asarray(y, dtype=float).ravel()
    n = W.shape[1]

    L = lipschitz if lipschitz is not None else power_iteration_lipschitz(W)
    x = np.zeros(n)
    z = x.copy()
    t = 1.0
    objective: list[float] = []
    converged = False
    iteration = 0

    for iteration in range(1, max_iter + 1):
        # gradient of the smooth term at the extrapolated point
        grad = W.T @ (W @ z - y)
        x_new = soft_threshold(z - grad / L, alpha / L)
        if non_negative:
            x_new = np.maximum(x_new, 0.0)

        # Nesterov momentum
        t_new = 0.5 * (1.0 + math.sqrt(1.0 + 4.0 * t * t))
        z = x_new + ((t - 1.0) / t_new) * (x_new - x)

        residual = W @ x_new - y
        value = 0.5 * float(residual @ residual) + alpha * float(np.abs(x_new).sum())
        objective.append(value)

        delta = float(np.linalg.norm(x_new - x)) / max(1e-12, float(np.linalg.norm(x_new)))
        x = x_new
        t = t_new
        if delta < tolerance:
            converged = True
            break

    return FistaResult(image=x, iterations=iteration, objective=objective,
                       converged=converged, lipschitz=L)


# ----------------------------------------------------------------------
# target extraction
# ----------------------------------------------------------------------
@dataclass
class RtiTarget:
    """A blob in the attenuation image, interpreted as a person."""

    x: float
    y: float
    intensity: float
    voxels: int

    def as_dict(self) -> dict:
        return {
            "x": round(self.x, 3),
            "y": round(self.y, 3),
            "intensity": round(self.intensity, 4),
            "voxels": self.voxels,
        }


def extract_targets(
    grid: RtiGrid,
    image: np.ndarray,
    threshold_ratio: float = 0.45,
    min_voxels: int = 1,
) -> list[RtiTarget]:
    """Flood-fill blobs above ``threshold_ratio * max`` -> intensity-weighted centroids."""
    img = grid.reshape(np.asarray(image, dtype=float))
    peak = float(img.max()) if img.size else 0.0
    if peak <= 1e-12:
        return []
    mask = img > threshold_ratio * peak
    visited = np.zeros_like(mask, dtype=bool)
    targets: list[RtiTarget] = []

    for j in range(mask.shape[0]):
        for i in range(mask.shape[1]):
            if not mask[j, i] or visited[j, i]:
                continue
            stack = [(j, i)]
            visited[j, i] = True
            cells: list[tuple[int, int]] = []
            while stack:
                cj, ci = stack.pop()
                cells.append((cj, ci))
                for dj in (-1, 0, 1):
                    for di in (-1, 0, 1):
                        nj, ni = cj + dj, ci + di
                        if 0 <= nj < mask.shape[0] and 0 <= ni < mask.shape[1]:
                            if mask[nj, ni] and not visited[nj, ni]:
                                visited[nj, ni] = True
                                stack.append((nj, ni))
            if len(cells) < min_voxels:
                continue
            weights = np.array([img[cj, ci] for cj, ci in cells])
            total = float(weights.sum())
            cx = sum(w * (grid.min_x + (ci + 0.5) * grid.resolution) for w, (_cj, ci) in zip(weights, cells)) / total
            cy = sum(w * (grid.min_y + (cj + 0.5) * grid.resolution) for w, (cj, _ci) in zip(weights, cells)) / total
            targets.append(RtiTarget(cx, cy, float(weights.max()), len(cells)))

    targets.sort(key=lambda t: t.intensity, reverse=True)
    return targets


# ----------------------------------------------------------------------
# online estimator
# ----------------------------------------------------------------------
class RtiProcessor:
    """Keeps the node mesh, baseline RSSI and the reconstruction warm.

    Typical use: every mesh node reports the RSSI it hears from every other
    node; the processor tracks a running baseline (empty-room calibration),
    forms the shadowing vector and reconstructs the attenuation image.
    """

    def __init__(
        self,
        grid: RtiGrid,
        nodes: dict[str, tuple[float, float]],
        alpha: float = 0.08,
        use_l1: bool = True,
        ellipse_width: float = 0.35,
    ) -> None:
        self.grid = grid
        self.node_ids = sorted(nodes)
        self.node_positions = np.array([nodes[k] for k in self.node_ids], dtype=float)
        self.alpha = alpha
        self.use_l1 = use_l1
        self.W, self.links = build_weight_matrix(grid, self.node_positions, ellipse_width=ellipse_width)
        self.lipschitz = power_iteration_lipschitz(self.W)
        self.link_index = {(self.node_ids[a], self.node_ids[b]): i for i, (a, b) in enumerate(self.links)}
        self.baseline = np.zeros(len(self.links))
        self._baseline_samples = 0
        self.last_image = np.zeros(grid.voxel_count)
        self.last_result: FistaResult | None = None

    def measurement_vector(self, rssi: dict[tuple[str, str], float]) -> np.ndarray:
        """Order a ``{(node_a, node_b): rssi_dbm}`` dict into the link vector."""
        y = np.zeros(len(self.links))
        for (a, b), value in rssi.items():
            index = self.link_index.get((a, b), self.link_index.get((b, a)))
            if index is not None:
                y[index] = float(value)
        return y

    def calibrate(self, rssi: dict[tuple[str, str], float]) -> int:
        """Accumulate an empty-room baseline (running mean)."""
        y = self.measurement_vector(rssi)
        self._baseline_samples += 1
        self.baseline += (y - self.baseline) / self._baseline_samples
        return self._baseline_samples

    @property
    def calibrated(self) -> bool:
        return self._baseline_samples > 0

    def update(self, rssi: dict[tuple[str, str], float], max_iter: int = 120) -> FistaResult:
        """Reconstruct from a fresh RSSI snapshot."""
        y = self.measurement_vector(rssi)
        # attenuation is a *drop* relative to the empty-room baseline
        shadowing = np.maximum(self.baseline - y, 0.0)
        if self.use_l1:
            result = reconstruct_l1_fista(
                self.W, shadowing, alpha=self.alpha, max_iter=max_iter, lipschitz=self.lipschitz
            )
        else:
            image = reconstruct_tikhonov(self.W, shadowing, alpha=self.alpha)
            result = FistaResult(image=np.maximum(image, 0.0), iterations=1, converged=True)
        self.last_image = result.image
        self.last_result = result
        return result

    def targets(self, threshold_ratio: float = 0.45) -> list[RtiTarget]:
        return extract_targets(self.grid, self.last_image, threshold_ratio=threshold_ratio)

    def as_dict(self, include_image: bool = True, max_voxels: int = 4096) -> dict:
        payload = {
            "grid": self.grid.as_dict(),
            "nodes": {k: list(v) for k, v in zip(self.node_ids, self.node_positions.tolist())},
            "links": len(self.links),
            "calibrated": self.calibrated,
            "targets": [t.as_dict() for t in self.targets()],
        }
        if self.last_result is not None:
            payload["iterations"] = self.last_result.iterations
            payload["converged"] = self.last_result.converged
            payload["sparsity"] = round(self.last_result.sparsity, 4)
        if include_image and self.last_image.size <= max_voxels:
            peak = float(np.max(self.last_image)) if self.last_image.size else 0.0
            payload["image"] = [round(float(v), 4) for v in self.last_image]
            payload["peak"] = round(peak, 4)
        return payload
