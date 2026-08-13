"""Occupancy mapping, wall extraction, glTF export and the SQLite store."""

from __future__ import annotations

import json
import math
import time

import numpy as np
import pytest

from aura.mapping import (
    OccupancyGrid,
    build_mesh,
    extract_walls,
    mesh_to_gltf,
    _bresenham,
)
from aura.storage import LocalVectorStore


# ----------------------------------------------------------------------
# occupancy grid
# ----------------------------------------------------------------------
def test_bresenham_is_connected():
    line = _bresenham(0, 0, 10, 4)
    assert line[0] == (0, 0)
    assert line[-1] == (10, 4)
    for (x0, y0), (x1, y1) in zip(line, line[1:]):
        assert max(abs(x1 - x0), abs(y1 - y0)) == 1


def test_grid_coordinate_roundtrip():
    grid = OccupancyGrid(size_m=20.0, resolution=0.1, origin=(-2.0, -2.0))
    for x, y in [(0.0, 0.0), (5.55, -1.25), (11.0, 3.3)]:
        cx, cy = grid.world_to_cell(x, y)
        wx, wy = grid.cell_to_world(cx, cy)
        assert abs(wx - x) <= grid.resolution
        assert abs(wy - y) <= grid.resolution


def test_integrate_scan_marks_hits_and_free_space():
    grid = OccupancyGrid(size_m=20.0, resolution=0.1, origin=(0.0, 0.0))
    origin = (5.0, 5.0)
    points = np.array([[9.0, 5.0], [5.0, 9.0], [1.0, 5.0]])
    for _ in range(5):
        grid.integrate_scan(origin, points)
    prob = grid.probability()
    hx, hy = grid.world_to_cell(9.0, 5.0)
    fx, fy = grid.world_to_cell(7.0, 5.0)
    assert prob[hy, hx] > 0.8, "the endpoint must become occupied"
    assert prob[fy, fx] < 0.3, "space along the ray must be carved free"


def test_grid_stats_and_point_cloud():
    grid = OccupancyGrid(size_m=20.0, resolution=0.1, origin=(0.0, 0.0))
    points = np.array([[9.0, y] for y in np.arange(2.0, 8.0, 0.1)])
    for _ in range(4):
        grid.integrate_scan((5.0, 5.0), points)
    stats = grid.stats()
    assert stats.cells_occupied > 30
    assert 0.0 < stats.coverage < 1.0
    cloud = grid.point_cloud(max_points=300, layers=3)
    assert cloud and len(cloud) <= 300 + 3
    assert all(len(p) == 4 for p in cloud)


def test_grid_serialisation_roundtrip():
    grid = OccupancyGrid(size_m=10.0, resolution=0.2, origin=(0.0, 0.0))
    grid.integrate_scan((2.0, 2.0), np.array([[6.0, 2.0], [2.0, 6.0]]))
    blob = grid.snapshot_bytes()
    restored = OccupancyGrid(size_m=10.0, resolution=0.2, origin=(0.0, 0.0))
    restored.load_bytes(blob)
    assert np.allclose(grid.grid, restored.grid)


def test_navigation_grid_inflates_obstacles():
    grid = OccupancyGrid(size_m=10.0, resolution=0.2, origin=(0.0, 0.0))
    for _ in range(6):
        grid.integrate_scan((1.0, 1.0), np.array([[5.0, 5.0]]))
    nav = grid.navigation_grid(inflate=2)
    cx, cy = grid.world_to_cell(5.0, 5.0)
    assert not nav[cy, cx]
    assert not nav[cy + 1, cx]          # inflated
    assert nav[cy + 6, cx]              # far enough away


# ----------------------------------------------------------------------
# wall extraction / mesh
# ----------------------------------------------------------------------
def test_extract_walls_finds_a_straight_line():
    xs = np.arange(0.0, 6.0, 0.05)
    points = np.column_stack([xs, np.full_like(xs, 3.0) + np.random.default_rng(1).normal(0, 0.01, len(xs))])
    segments = extract_walls(points, min_support=10)
    assert segments
    longest = segments[0]
    assert longest.length == pytest.approx(6.0, abs=0.3)
    assert abs(longest.y1 - 3.0) < 0.1 and abs(longest.y2 - 3.0) < 0.1


def test_extract_walls_handles_two_perpendicular_walls():
    a = np.column_stack([np.arange(0, 5, 0.05), np.zeros(100)])
    b = np.column_stack([np.zeros(100), np.arange(0, 5, 0.05)])
    segments = extract_walls(np.vstack([a, b]), min_support=15)
    assert len(segments) >= 2
    assert sum(s.length for s in segments[:2]) == pytest.approx(10.0, abs=1.0)


def test_extract_walls_on_empty_input():
    assert extract_walls(np.zeros((0, 2))) == []


def test_build_mesh_produces_valid_topology():
    from aura.mapping import Segment

    mesh = build_mesh([Segment(0.0, 0.0, 5.0, 0.0)], height=2.5)
    assert mesh["vertex_count"] > 0
    assert mesh["triangle_count"] == len(mesh["indices"]) // 3
    assert len(mesh["positions"]) == mesh["vertex_count"] * 3
    assert len(mesh["normals"]) == len(mesh["positions"])
    assert max(mesh["indices"]) < mesh["vertex_count"]
    zs = mesh["positions"][2::3]
    assert min(zs) == pytest.approx(0.0)
    assert max(zs) == pytest.approx(2.5)


def test_gltf_export_is_wellformed():
    from aura.mapping import Segment

    mesh = build_mesh([Segment(0.0, 0.0, 4.0, 0.0), Segment(4.0, 0.0, 4.0, 3.0)])
    gltf = mesh_to_gltf(mesh, name="TestScan")
    assert gltf["asset"]["version"] == "2.0"
    assert gltf["meshes"][0]["primitives"][0]["attributes"]["POSITION"] == 0
    assert gltf["buffers"][0]["uri"].startswith("data:application/octet-stream;base64,")
    assert len(gltf["accessors"]) == 3
    assert gltf["accessors"][0]["count"] == mesh["vertex_count"]
    assert gltf["accessors"][2]["count"] == len(mesh["indices"])
    json.dumps(gltf)  # must be serialisable


def test_gltf_export_survives_empty_mesh():
    gltf = mesh_to_gltf({"positions": [], "normals": [], "indices": [], "vertex_count": 0, "triangle_count": 0})
    assert gltf["buffers"][0]["byteLength"] > 0


# ----------------------------------------------------------------------
# storage
# ----------------------------------------------------------------------
@pytest.fixture()
def store(tmp_path):
    s = LocalVectorStore(tmp_path / "test.db", project="unit-test")
    yield s
    s.close()


def test_store_uses_wal(store):
    assert store.stats()["journal_mode"].lower() == "wal"


def test_save_and_read_transforms(store):
    for i in range(5):
        store.save_transform(
            {"offset_x": float(i), "offset_y": 1.0, "offset_z": 1.4, "roll": 0.0, "pitch": 0.0, "yaw": 0.1 * i},
            covariance=[0.1] * 9,
            velocity=[0.5, 0.0, 0.0],
            metadata={"iteration": i},
            timestamp=time.time() + i,
        )
    history = store.history(limit=10)
    assert len(history) == 5
    assert history[0]["transform"]["offset_x"] == 0.0      # ascending order
    assert history[-1]["transform"]["offset_x"] == 4.0
    assert history[0]["metadata"]["iteration"] == 0
    assert len(history[0]["covariance"]) == 9


def test_history_since_filter(store):
    now = time.time()
    store.save_transform({"offset_x": 1.0}, timestamp=now - 100)
    store.save_transform({"offset_x": 2.0}, timestamp=now)
    recent = store.history(since=now - 10)
    assert len(recent) == 1
    assert recent[0]["transform"]["offset_x"] == 2.0


def test_events_roundtrip(store):
    store.save_event("ble", {"rssi": -60})
    store.save_event("uwb", {"range": 4.2})
    assert len(store.events()) == 2
    ble_only = store.events(source="ble")
    assert len(ble_only) == 1
    assert ble_only[0]["payload"]["rssi"] == -60


def test_map_versioning(store):
    grid = OccupancyGrid(size_m=10.0, resolution=0.5)
    store.save_map(grid.snapshot_bytes(), 0.5, (0.0, 0.0), grid.cells, {"coverage": 0.1})
    grid.integrate_scan((1.0, 1.0), np.array([[4.0, 4.0]]))
    store.save_map(grid.snapshot_bytes(), 0.5, (0.0, 0.0), grid.cells, {"coverage": 0.2})
    versions = store.map_versions()
    assert len(versions) == 2
    assert versions[0]["version"] > versions[1]["version"]
    latest = store.load_map()
    assert latest["stats"]["coverage"] == 0.2
    older = store.load_map(version=versions[1]["version"])
    assert older["stats"]["coverage"] == 0.1


def test_scenario_lifecycle(store):
    run_id = store.start_scenario("evacuation", {"people": 20})
    runs = store.scenario_runs()
    assert runs[0]["status"] == "running"
    store.finish_scenario(run_id, {"escaped": 18}, status="completed")
    runs = store.scenario_runs()
    assert runs[0]["status"] == "completed"
    assert runs[0]["result"]["escaped"] == 18
    assert runs[0]["finished_at"] is not None


def test_retention_by_age_and_count(store):
    now = time.time()
    for i in range(20):
        store.save_transform({"offset_x": float(i)}, timestamp=now - (2000 if i < 10 else 0))
    removed = store.enforce_retention(max_age_seconds=1000, max_records=100_000)
    assert removed["transforms"] == 10
    assert len(store.history(limit=100)) == 10

    removed = store.enforce_retention(max_age_seconds=10_000, max_records=4)
    assert removed["transforms"] == 6
    assert len(store.history(limit=100)) == 4


def test_projects_are_isolated(store):
    other = store.ensure_project("second-site")
    store.save_transform({"offset_x": 1.0})
    store.save_transform({"offset_x": 9.0}, project_id=other)
    assert len(store.history()) == 1
    assert len(store.history(project_id=other)) == 1
    names = [p["name"] for p in store.list_projects()]
    assert "unit-test" in names and "second-site" in names


def test_stats_reports_counts(store):
    store.save_transform({"offset_x": 1.0})
    store.save_event("test", {})
    stats = store.stats()
    assert stats["transforms"] == 1
    assert stats["events"] == 1
    assert stats["size_bytes"] > 0
