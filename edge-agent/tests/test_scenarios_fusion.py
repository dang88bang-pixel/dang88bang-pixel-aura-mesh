"""Scenario engine and end-to-end fusion pipeline tests."""

from __future__ import annotations

import math

import numpy as np
import pytest

from aura.config import AgentConfig
from aura.fusion import FusionPipeline, PersonTracker, ScanMatcher
from aura.mapping import OccupancyGrid
from aura.scenarios import (
    FlowField,
    ScenarioEngine,
    ScenarioParams,
    ScenarioRun,
    SmokeField,
)
from aura.world import WORLD


# ----------------------------------------------------------------------
# scenario primitives
# ----------------------------------------------------------------------
def test_scenario_params_clamping_and_defaults():
    params = ScenarioParams.from_dict({"scenario": "nonsense", "people": 9999, "smoke_density": 5.0, "panic": -1})
    assert params.scenario == "evacuation"
    assert params.people == 400
    assert params.smoke_density == 1.0
    assert params.panic == 0.0
    assert ScenarioParams.from_dict({"type": "tactical"}).scenario == "tactical"
    assert ScenarioParams.from_dict({"fire_source": [3, 4]}).fire_source == (3.0, 4.0)


def test_flow_field_points_towards_exit():
    flow = FlowField(WORLD)
    flow.compute([(10.0, -0.4)])
    # from the middle of the corridor the descent must head south towards the exit
    direction = flow.direction(10.0, 5.0)
    assert np.linalg.norm(direction) > 0
    assert direction[1] < 0
    near = flow.distance_at(10.0, 1.0)
    far = flow.distance_at(3.5, 11.0)
    assert near < far


def test_flow_field_distance_is_finite_in_rooms():
    flow = FlowField(WORLD)
    flow.compute([(10.0, -0.4), (20.4, 7.0)])
    for point in [(3.5, 11.0), (16.0, 11.0), (2.8, 2.8), (10.0, 6.5)]:
        assert math.isfinite(flow.distance_at(*point)), f"{point} must reach an exit"


def test_smoke_field_diffuses_and_is_blocked_by_walls():
    smoke = SmokeField(WORLD, resolution=0.5)
    smoke.set_source(10.0, 6.5, 1.0)
    for _ in range(40):
        smoke.step(0.1)
    assert smoke.at(10.0, 6.5) > 0.05
    assert smoke.field.max() <= 1.0
    payload = smoke.as_dict()
    assert payload["cells"]
    assert 0.0 < payload["max_concentration"] <= 1.0


def test_evacuation_run_completes_and_reports_metrics():
    params = ScenarioParams.from_dict(
        {"scenario": "evacuation", "people": 15, "duration": 300, "smoke_density": 0.2, "seed": 7}
    )
    run = ScenarioRun(params, WORLD)
    assert run.total_agents == 15
    for _ in range(3000):
        run.step(0.1)
        if run.finished:
            break
    assert run.finished
    metrics = run.metrics
    assert metrics["total_agents"] == 15
    assert metrics["escaped"] >= 12, f"most agents should find an exit, got {metrics}"
    assert metrics["escaped"] + metrics["casualties"] + metrics["stranded"] == 15
    assert metrics["evacuation_time_p50"] is not None
    assert metrics["sim_time"] > 0


def test_agents_never_walk_through_walls():
    params = ScenarioParams.from_dict({"scenario": "evacuation", "people": 20, "duration": 60, "seed": 3})
    run = ScenarioRun(params, WORLD)
    for _ in range(600):
        run.step(0.05)
        for agent in run.agents:
            if agent.escaped:
                continue
            x, y = float(agent.position[0]), float(agent.position[1])
            assert WORLD.is_free(x, y, 0.05), f"agent walked into a wall at ({x:.2f}, {y:.2f})"
        if run.finished:
            break


def test_smoke_exposure_accumulates_on_the_escape_route():
    # Put the fire right on the corridor everyone has to pass through,
    # otherwise the agents evacuate before the smoke ever reaches them.
    common = {"scenario": "evacuation", "people": 20, "duration": 400, "seed": 11,
              "fire_source": [10.0, 4.0]}
    clean = ScenarioRun(ScenarioParams.from_dict({**common, "smoke_density": 0.0}), WORLD)
    smoky = ScenarioRun(ScenarioParams.from_dict({**common, "smoke_density": 1.0}), WORLD)
    for run in (clean, smoky):
        for _ in range(4000):
            run.step(0.1)
            if run.finished:
                break
    assert clean.metrics["mean_smoke_dose"] == 0.0
    assert smoky.metrics["mean_smoke_dose"] > 0.0
    assert smoky.metrics["peak_smoke"] > 0.1
    assert smoky.metrics["escaped"] >= 1


def test_zero_people_finishes_immediately():
    run = ScenarioRun(ScenarioParams.from_dict({"people": 0, "duration": 30}), WORLD)
    run.step(0.1)
    assert run.finished
    assert run.metrics["total_agents"] == 0


def test_scenario_engine_start_stop_pause():
    engine = ScenarioEngine(WORLD)
    engine.start(ScenarioParams.from_dict({"people": 5, "duration": 100}))
    assert engine.active is not None
    engine.step(0.1)
    assert engine.pause(True)
    before = engine.active.sim_time
    engine.step(0.1)
    assert engine.active.sim_time == before, "a paused run must not advance"
    engine.pause(False)
    engine.step(0.1)
    assert engine.active.sim_time > before
    metrics = engine.stop()
    assert metrics is not None
    assert engine.history


def test_scenario_snapshot_is_json_friendly():
    engine = ScenarioEngine(WORLD)
    engine.start(ScenarioParams.from_dict({"people": 6, "smoke_density": 0.4}))
    snapshot = engine.step(0.2)
    import json

    json.dumps(snapshot)
    assert snapshot["total_agents"] == 6
    assert "agents" in snapshot and "smoke" in snapshot
    assert 0.0 <= snapshot["progress"] <= 1.0


# ----------------------------------------------------------------------
# tracker / matcher
# ----------------------------------------------------------------------
def test_person_tracker_associates_and_expires():
    tracker = PersonTracker()
    now = 1000.0
    tracker.update([(1.0, 1.0, "mmwave", False)], now)
    tracks = tracker.update([(1.1, 1.05, "mmwave", False)], now + 0.2)
    assert len(tracks) == 1
    track = tracks[0]
    assert track.hits == 2
    assert 0.0 < track.confidence <= 1.0
    # a second, distant person must create a separate track
    tracker.update([(1.2, 1.1, "mmwave", False), (8.0, 8.0, "thermal", False)], now + 0.4)
    tracker.update([(1.3, 1.15, "mmwave", False), (8.1, 8.05, "thermal", False)], now + 0.6)
    assert len(tracker.tracks) == 2
    assert tracker.update([], now + 100.0) == []


def test_scan_matcher_recovers_a_known_offset():
    grid = OccupancyGrid(size_m=24.0, resolution=0.1, origin=(0.0, 0.0))
    truth = (8.0, 6.0, 0.0)
    angles = np.linspace(-math.pi, math.pi, 240, endpoint=False)
    distances = np.array([WORLD.raycast((truth[0], truth[1]), a, 16.0) for a in angles])
    keep = distances < 15.9
    angles, distances = angles[keep], distances[keep]
    points = np.column_stack([truth[0] + distances * np.cos(angles), truth[1] + distances * np.sin(angles)])
    for _ in range(10):   # the matcher requires a map with >= 8 integrations
        grid.integrate_scan((truth[0], truth[1]), points)

    matcher = ScanMatcher(grid)
    body = np.column_stack([distances * np.cos(angles), distances * np.sin(angles)])
    result = matcher.match((truth[0] - 0.2, truth[1] + 0.2, 0.0), body)
    assert result is not None
    mx, my, myaw, score = result
    assert math.hypot(mx - truth[0], my - truth[1]) < 0.16
    assert score > 0.3


def test_scan_matcher_declines_on_an_empty_map():
    grid = OccupancyGrid(size_m=10.0, resolution=0.1)
    matcher = ScanMatcher(grid)
    body = np.random.default_rng(0).normal(0, 2, (100, 2))
    assert matcher.match((0.0, 0.0, 0.0), body) is None


# ----------------------------------------------------------------------
# full pipeline
# ----------------------------------------------------------------------
@pytest.fixture()
def pipeline(tmp_path):
    cfg = AgentConfig()
    cfg.db_path = str(tmp_path / "pipeline.db")
    cfg.simulate = True
    cfg.persist_every = 2
    p = FusionPipeline(cfg)
    for driver in p.drivers:
        driver.open()
    yield p
    p.store.close()


def test_pipeline_tick_produces_a_complete_frame(pipeline):
    frame = pipeline.tick()
    for key in ("ekf", "transform", "pose", "map", "device", "people", "vitals"):
        assert key in frame, f"missing telemetry key {key}"
    assert frame["device"]["battery"] <= 100.0
    import json

    json.dumps(frame, default=str)


def test_pipeline_localises_against_ground_truth(pipeline):
    errors = []
    for i in range(300):
        frame = pipeline.tick()
        truth = pipeline.imu.ground_truth()
        if truth and i > 60:
            errors.append(math.hypot(frame["pose"]["x"] - truth[0], frame["pose"]["y"] - truth[1]))
    assert errors
    mean_error = float(np.mean(errors))
    p95 = float(np.percentile(errors, 95))
    assert mean_error < 0.75, f"mean localisation error too high: {mean_error:.2f} m"
    assert p95 < 1.2, f"p95 localisation error too high: {p95:.2f} m"


def test_pipeline_keeps_altitude_bounded(pipeline):
    for _ in range(200):
        pipeline.tick()
    z = pipeline.ekf.position[2]
    assert 0.5 < z < 2.5, f"altitude drifted to {z:.2f} m"


def test_pipeline_builds_a_map_and_extracts_walls(pipeline):
    for _ in range(220):
        pipeline.tick()
    stats = pipeline.grid.stats()
    assert stats.cells_occupied > 200
    assert stats.updates > 100
    assert pipeline.walls, "wall extraction should have produced segments"
    mesh = pipeline.mesh()
    assert mesh["triangle_count"] > 0


def test_pipeline_persists_transforms(pipeline):
    for _ in range(20):
        pipeline.tick()
    history = pipeline.history(limit=100)
    assert len(history) >= 9      # persist_every = 2
    assert "transform" in history[0]


def test_pipeline_runs_a_scenario_and_detects_people(pipeline):
    pipeline.start_scenario(ScenarioParams.from_dict({"scenario": "evacuation", "people": 25, "duration": 200}))
    seen = 0
    for _ in range(150):
        frame = pipeline.tick()
        seen = max(seen, len(frame["people"]))
    assert seen > 0, "mmWave/thermal should detect at least one moving person"
    assert pipeline.scenarios.active is not None
    metrics = pipeline.stop_scenario()
    assert metrics is not None and metrics["total_agents"] == 25


def test_pipeline_subscribers_receive_frames(pipeline):
    received: list[dict] = []
    unsubscribe = pipeline.subscribe(received.append)
    for _ in range(40):
        pipeline.tick()
    assert received, "the broadcast throttle should still emit frames"
    assert received[0]["type"] == "telemetry"
    unsubscribe()
    count = len(received)
    for _ in range(40):
        pipeline.tick()
    assert len(received) == count, "unsubscribe must stop delivery"


def test_pipeline_survives_a_failing_subscriber(pipeline):
    def boom(_frame: dict) -> None:
        raise RuntimeError("subscriber exploded")

    pipeline.subscribe(boom)
    for _ in range(30):
        pipeline.tick()   # must not raise


def test_pipeline_state_and_map_payload(pipeline):
    for _ in range(30):
        pipeline.tick()
    state = pipeline.state()
    assert set(state) >= {"ekf", "sensors", "map", "diagnostics", "storage", "config"}
    assert len(state["sensors"]) == 6
    payload = pipeline.map_payload(max_points=500)
    assert "points" in payload and "ground_truth" in payload


def test_pipeline_save_and_load_map(pipeline):
    for _ in range(40):
        pipeline.tick()
    saved = pipeline.save_map()
    assert saved["map_id"]
    before = pipeline.grid.grid.copy()
    pipeline.grid.grid[:] = 0.0
    assert pipeline.load_map()
    assert np.allclose(pipeline.grid.grid, before)


def test_pipeline_config_patch_switches_mmwave_profile(pipeline):
    changed = pipeline.apply_config({"loop_hz": 12.0, "sensors": {"mmwave_reduced": True}})
    assert "loop_hz" in changed
    assert pipeline.config.loop_hz == 12.0
    assert pipeline.mmwave.reduced is True


def test_pipeline_start_and_stop_threaded(pipeline):
    import time

    pipeline.start()
    time.sleep(0.6)
    iterations = pipeline._iterations
    assert iterations > 2, "the background loop should be ticking"
    pipeline.stop()
    time.sleep(0.2)
    assert pipeline._iterations >= iterations
