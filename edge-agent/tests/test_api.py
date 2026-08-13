"""End-to-end API tests: REST surface, auth, WebSocket and the AURA 6.0 routes."""

from __future__ import annotations

import json
import math

import pytest
from fastapi.testclient import TestClient

from aura.api import create_app
from aura.config import AgentConfig


@pytest.fixture()
def client(tmp_path):
    config = AgentConfig()
    config.db_path = str(tmp_path / "api.db")
    config.simulate = True
    config.project = "api-test"
    app = create_app(config, autostart=False)
    with TestClient(app) as test_client:
        # tick the pipeline by hand so tests stay deterministic
        pipeline = app.state.pipeline
        for driver in pipeline.drivers:
            driver.open()
        for _ in range(25):
            pipeline.tick()
        yield test_client


@pytest.fixture()
def secured_client(tmp_path):
    config = AgentConfig()
    config.db_path = str(tmp_path / "secure.db")
    config.simulate = True
    config.api_token = "s3cret"
    app = create_app(config, autostart=False)
    with TestClient(app) as test_client:
        yield test_client


# ----------------------------------------------------------------------
# meta
# ----------------------------------------------------------------------
def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] in {"ok", "degraded"}
    assert set(body["sensors"]) == {"lidar", "mmwave", "uwb", "ble", "imu", "thermal"}


def test_info_lists_endpoints(client):
    body = client.get("/api/v1/agent/info").json()
    assert body["websocket"] == "/ws/agent/events"
    assert "evacuation" in body["scenarios"]
    assert any(path.endswith("/rti/configure") for path in body["endpoints"])


def test_openapi_is_valid(client):
    spec = client.get("/api/openapi.json").json()
    assert spec["info"]["title"] == "Aura Edge Agent"
    assert "/api/v1/agent/state" in spec["paths"]


# ----------------------------------------------------------------------
# auth
# ----------------------------------------------------------------------
def test_token_is_enforced(secured_client):
    assert secured_client.get("/api/v1/agent/state").status_code == 401
    assert secured_client.get(
        "/api/v1/agent/state", headers={"Authorization": "Bearer wrong"}
    ).status_code == 401
    assert secured_client.get(
        "/api/v1/agent/state", headers={"Authorization": "Bearer s3cret"}
    ).status_code == 200
    # health stays open so a load balancer can probe it
    assert secured_client.get("/health").status_code == 200


def test_websocket_rejects_a_bad_token(secured_client):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with secured_client.websocket_connect("/ws/agent/events?token=nope") as ws:
            ws.receive_text()


# ----------------------------------------------------------------------
# state / map
# ----------------------------------------------------------------------
def test_state_shape(client):
    body = client.get("/api/v1/agent/state").json()
    assert {"ekf", "transform", "sensors", "map", "diagnostics", "storage", "config"} <= set(body)
    assert len(body["ekf"]["position"]) == 3
    assert len(body["ekf"]["covariance_diagonal"]) == 15


def test_history_and_limit(client):
    body = client.get("/api/v1/agent/history?limit=5").json()
    assert body["count"] <= 5
    if body["records"]:
        assert "transform" in body["records"][0]


def test_map_payload(client):
    body = client.get("/api/v1/agent/map?max_points=500").json()
    assert "points" in body and "stats" in body and "ground_truth" in body
    assert len(body["points"]) <= 500 + 3


def test_map_save_and_versions(client):
    saved = client.post("/api/v1/agent/map/save").json()
    assert saved["map_id"]
    versions = client.get("/api/v1/agent/map/versions").json()["versions"]
    assert len(versions) >= 1


def test_gltf_export_is_downloadable(client):
    response = client.get("/api/v1/agent/export/gltf")
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    gltf = json.loads(response.content)
    assert gltf["asset"]["version"] == "2.0"
    assert gltf["buffers"][0]["uri"].startswith("data:application/octet-stream;base64,")


def test_json_export(client):
    body = json.loads(client.get("/api/v1/agent/export/json?limit=10").content)
    assert {"state", "map", "history", "scenarios"} <= set(body)


# ----------------------------------------------------------------------
# scenarios
# ----------------------------------------------------------------------
def test_scenario_lifecycle(client):
    started = client.post("/api/v1/agent/scenario/start", json={
        "scenario": "evacuation", "people": 10, "smoke_density": 0.3, "duration": 60,
    })
    assert started.status_code == 200
    assert started.json()["total_agents"] == 10

    assert client.post("/api/v1/agent/scenario/pause", json={"paused": True}).status_code == 200
    assert client.post("/api/v1/agent/scenario/pause", json={"paused": False}).status_code == 200

    stopped = client.post("/api/v1/agent/scenario/stop")
    assert stopped.status_code == 200
    assert stopped.json()["total_agents"] == 10
    # stopping twice is a conflict, not a crash
    assert client.post("/api/v1/agent/scenario/stop").status_code == 409


def test_unknown_scenario_rejected(client):
    response = client.post("/api/v1/agent/scenario/start", json={"scenario": "teleportation"})
    assert response.status_code == 400


def test_scenario_parameters_are_validated(client):
    assert client.post("/api/v1/agent/scenario/start", json={"people": -5}).status_code == 422
    assert client.post("/api/v1/agent/scenario/start", json={"smoke_density": 42}).status_code == 422


# ----------------------------------------------------------------------
# config / tokens / ingest
# ----------------------------------------------------------------------
def test_config_patch(client):
    body = client.post("/api/v1/agent/config", json={"loop_hz": 15.0}).json()
    assert "loop_hz" in body["changed"]
    assert client.get("/api/v1/agent/config").json()["loop_hz"] == 15.0


def test_token_crud(client):
    before = len(client.get("/api/v1/agent/tokens").json()["tokens"])
    client.post("/api/v1/agent/tokens", json={"address": "AA:BB:CC:00:99", "label": "Neu", "x": 5, "y": 5})
    assert len(client.get("/api/v1/agent/tokens").json()["tokens"]) == before + 1
    assert client.delete("/api/v1/agent/tokens/AA:BB:CC:00:99").status_code == 200
    assert client.delete("/api/v1/agent/tokens/AA:BB:CC:00:99").status_code == 404


def test_multi_device_ingest(client):
    response = client.post("/api/v1/agent/ingest", json={
        "device_id": "ct45p-02",
        "rssi": {"AA:BB:CC:00:01": -62},
        "position": [3.0, 4.0, 1.4],
        "battery": 81.0,
    })
    assert response.json()["accepted"]
    devices = client.get("/api/v1/agent/devices").json()["devices"]
    assert any(d["device_id"] == "ct45p-02" for d in devices)


# ----------------------------------------------------------------------
# AURA 6.0: RTI
# ----------------------------------------------------------------------
def ring(count: int, cx=3.0, cy=3.0, radius=3.2):
    return [
        {"id": f"n{i}", "x": cx + radius * math.cos(2 * math.pi * i / count),
         "y": cy + radius * math.sin(2 * math.pi * i / count)}
        for i in range(count)
    ]


def test_rti_requires_configuration_first(client):
    assert client.get("/api/v1/agent/rti").json()["configured"] is False
    assert client.post("/api/v1/agent/rti/measure", json={"rssi": {"a|b": -50}}).status_code == 409


def test_rti_rejects_too_few_nodes(client):
    response = client.post("/api/v1/agent/rti/configure", json={"nodes": ring(2)})
    assert response.status_code == 400


def test_rti_rejects_an_oversized_grid(client):
    response = client.post("/api/v1/agent/rti/configure", json={
        "nodes": ring(6), "max_x": 200, "max_y": 200, "resolution": 0.1,
    })
    assert response.status_code == 400
    assert "too large" in response.json()["detail"]


def test_rti_configure_calibrate_and_detect(client):
    configured = client.post("/api/v1/agent/rti/configure", json={
        "nodes": ring(12), "min_x": 0, "min_y": 0, "max_x": 6, "max_y": 6,
        "resolution": 0.25, "alpha": 0.05,
    }).json()
    assert configured["configured"]
    assert configured["links"] == 66
    assert configured["voxels"] == 576
    assert configured["lipschitz"] > 0

    baseline = {f"n{i}|n{j}": -50.0 for i in range(12) for j in range(i + 1, 12)}
    calibrated = client.post("/api/v1/agent/rti/measure", json={"rssi": baseline, "calibrate": True}).json()
    assert calibrated["calibrated"]

    # an empty room must yield no targets
    empty = client.post("/api/v1/agent/rti/measure", json={"rssi": baseline}).json()
    assert empty["targets"] == []

    # now attenuate the links that cross (2, 4)
    import numpy as np

    from aura.rti import RtiGrid, build_weight_matrix

    grid = RtiGrid(0, 0, 6, 6, 0.25)
    nodes = np.array([[n["x"], n["y"]] for n in ring(12)])
    W, links = build_weight_matrix(grid, nodes)
    truth = np.zeros(grid.voxel_count)
    truth[grid.index_of(2.0, 4.0)] = 1.0
    attenuation = W @ truth

    occupied = dict(baseline)
    for index, (a, b) in enumerate(links):
        occupied[f"n{a}|n{b}"] = -50.0 - float(attenuation[index])

    result = client.post("/api/v1/agent/rti/measure", json={"rssi": occupied}).json()
    assert result["targets"], "a person in the room must produce a target"
    target = result["targets"][0]
    assert math.hypot(target["x"] - 2.0, target["y"] - 4.0) < 0.8


# ----------------------------------------------------------------------
# AURA 6.0: passive radar
# ----------------------------------------------------------------------
def test_radar_limits_are_physical(client):
    body = client.get("/api/v1/agent/radar/limits?bandwidth_hz=2400000").json()
    assert body["range_resolution_m"] == pytest.approx(62.5, rel=0.01)
    wide = client.get("/api/v1/agent/radar/limits?bandwidth_hz=8000000").json()
    assert wide["range_resolution_m"] < body["range_resolution_m"]


def test_radar_process_detects_the_injected_target(client):
    body = client.post("/api/v1/agent/radar/process", json={
        "samples": 32768,
        "targets": [[20, 400.0, 0.03]],
        "eca_taps": 16,
        "max_range_bins": 48,
        "num_batches": 64,
        "threshold_db": 8.0,
    }).json()
    assert "detections" in body and "resolution" in body
    assert body["resolution"]["range_m"] == pytest.approx(62.5, rel=0.01)
    bins = {d["range_bin"] for d in body["detections"]}
    assert 20 in bins, f"expected range bin 20, got {sorted(bins)}"


def test_radar_map_is_serialisable(client):
    body = client.post("/api/v1/agent/radar/process", json={"samples": 8192, "num_batches": 16}).json()
    json.dumps(body)
    assert body["map"]["shape"][0] == len(body["map"]["doppler_bins"])


# ----------------------------------------------------------------------
# AURA 6.0: voxels
# ----------------------------------------------------------------------
def test_voxel_ingest_and_query(client):
    points = [[float(i) * 0.1, 1.0, 0.5] for i in range(50)]
    ingested = client.post("/api/v1/agent/voxels/ingest?label=2", json=points).json()
    assert ingested["ingested"] == 50
    body = client.get("/api/v1/agent/voxels?threshold=100").json()
    assert body["stats"]["occupied_voxels"] > 0
    assert body["points"]
    assert all(point[4] == 2 for point in body["points"])
    assert "person" in body["labels"].values()


# ----------------------------------------------------------------------
# AURA 6.0: audit chain
# ----------------------------------------------------------------------
def test_audit_records_startup_and_verifies(client):
    verification = client.get("/api/v1/agent/audit/verify").json()
    assert verification["valid"]
    entries = client.get("/api/v1/agent/audit").json()
    assert entries["stats"]["count"] >= 1
    assert any(entry["action"] == "agent.start" for entry in entries["entries"])


def test_audit_append_and_chain_integrity(client):
    for i in range(5):
        response = client.post("/api/v1/agent/audit", json={
            "actor": "operator", "action": "marker.place",
            "payload": {"index": i}, "severity": "notice",
        })
        assert response.status_code == 200
        assert response.json()["chain_hash"]

    body = client.get("/api/v1/agent/audit/verify").json()
    assert body["valid"], body["message"]

    entries = client.get("/api/v1/agent/audit?limit=100").json()["entries"]
    markers = [e for e in entries if e["action"] == "marker.place"]
    assert len(markers) == 5
    # links must chain
    for previous, current in zip(markers, markers[1:]):
        assert current["prev_hash"] == previous["chain_hash"]


def test_audit_concurrent_posts_while_fusion_writes(tmp_path):
    """The CI integration check that 500'd: POST /audit while the loop runs.

    FastAPI runs sync handlers on a thread pool, the fusion loop writes
    transforms on another thread, both against one SQLite connection.
    Without the shared lock a single POST can raise SystemError and the
    handler returns 500 — exactly what ``curl .../audit`` hit.
    """
    import threading

    config = AgentConfig()
    config.db_path = str(tmp_path / "live-audit.db")
    config.simulate = True
    config.project = "live-audit"
    app = create_app(config, autostart=False)
    pipeline = app.state.pipeline
    for driver in pipeline.drivers:
        driver.open()

    stop = threading.Event()

    def tick() -> None:
        while not stop.is_set():
            pipeline.tick()

    fusion = threading.Thread(target=tick, name="fusion-tick", daemon=True)
    fusion.start()
    try:
        with TestClient(app) as test_client:
            statuses: list[int] = []
            errors: list[str] = []

            def post(n: int) -> None:
                try:
                    response = test_client.post(
                        "/api/v1/agent/audit",
                        json={"actor": "ci", "action": "ci.check", "payload": {"n": n}},
                    )
                    statuses.append(response.status_code)
                except Exception as exc:  # noqa: BLE001
                    errors.append(repr(exc))

            workers = [threading.Thread(target=post, args=(n,)) for n in range(40)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()

            assert errors == [], f"client raised: {errors[:3]}"
            assert statuses.count(200) == 40, f"statuses={statuses}"
            body = test_client.get("/api/v1/agent/audit/verify").json()
            assert body["valid"], body
    finally:
        stop.set()
        fusion.join(timeout=5)
        for driver in pipeline.drivers:
            driver.close()


def test_audit_severity_filter(client):
    client.post("/api/v1/agent/audit", json={"action": "x", "severity": "security"})
    filtered = client.get("/api/v1/agent/audit?severity=security").json()["entries"]
    assert filtered
    assert all(entry["severity"] == "security" for entry in filtered)


# ----------------------------------------------------------------------
# websocket
# ----------------------------------------------------------------------
def test_websocket_handshake_and_commands(client):
    with client.websocket_connect("/ws/agent/events") as ws:
        hello = json.loads(ws.receive_text())
        assert hello["type"] == "hello"
        assert "world" in hello

        ws.send_text(json.dumps({"command": "ping"}))
        # the first telemetry frame may arrive before the pong
        for _ in range(5):
            message = json.loads(ws.receive_text())
            if message["type"] == "pong":
                break
        else:
            pytest.fail("no pong received")

        ws.send_text(json.dumps({"command": "state"}))
        for _ in range(5):
            message = json.loads(ws.receive_text())
            if message["type"] == "state":
                assert "ekf" in message["payload"]
                break
        else:
            pytest.fail("no state received")


def test_websocket_rejects_garbage(client):
    with client.websocket_connect("/ws/agent/events") as ws:
        ws.receive_text()
        ws.send_text("not json at all")
        for _ in range(5):
            message = json.loads(ws.receive_text())
            if message["type"] == "error":
                break
        else:
            pytest.fail("no error reported for malformed JSON")


def test_websocket_unknown_command(client):
    with client.websocket_connect("/ws/agent/events") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"command": "self_destruct"}))
        for _ in range(5):
            message = json.loads(ws.receive_text())
            if message["type"] == "error":
                assert "unknown command" in message["message"]
                break
        else:
            pytest.fail("unknown command not reported")


# ---------------------------------------------------------------------------
# UWB TDoA
# ---------------------------------------------------------------------------

_TDOA_ANCHORS = [
    {"id": "a0", "x": 0.0, "y": 0.0},
    {"id": "a1", "x": 10.0, "y": 0.0},
    {"id": "a2", "x": 10.0, "y": 8.0},
    {"id": "a3", "x": 0.0, "y": 8.0},
]


def _tdoa_for(truth=(3.5, 5.5)):
    import math
    pos = {a["id"]: (a["x"], a["y"]) for a in _TDOA_ANCHORS}
    r0 = math.dist(truth, pos["a0"])
    return {k: math.dist(truth, pos[k]) - r0 for k in ("a1", "a2", "a3")}


def test_tdoa_solves_with_wired_class_sync(client):
    response = client.post("/api/v1/agent/uwb/tdoa", json={
        "anchors": _TDOA_ANCHORS, "tdoa_m": _tdoa_for(), "sync_sigma_ns": 0.1,
    })
    assert response.status_code == 200
    body = response.json()
    assert body["usable_sync"] is True
    assert body["warning"] is None
    assert body["fix"]["x"] == pytest.approx(3.5, abs=0.01)
    assert body["fix"]["y"] == pytest.approx(5.5, abs=0.01)
    assert body["fix"]["anchors_used"] == 4


def test_tdoa_withholds_the_fix_when_sync_is_unusable(client):
    """NTP-class sync must not yield a plottable position."""
    response = client.post("/api/v1/agent/uwb/tdoa", json={
        "anchors": _TDOA_ANCHORS, "tdoa_m": _tdoa_for(), "sync_sigma_ns": 1_000_000.0,
    })
    assert response.status_code == 200
    body = response.json()
    assert body["fix"] is None, "unusable sync must not produce a renderable fix"
    assert body["usable_sync"] is False
    assert body["diagnostic_only_fix"] is not None
    assert body["sync_range_sigma_m"] > 1000.0
    assert "wired backbone" in body["warning"]


def test_tdoa_sigma_carries_the_sync_term(client):
    def sigma(sync_ns):
        return client.post("/api/v1/agent/uwb/tdoa", json={
            "anchors": _TDOA_ANCHORS, "tdoa_m": _tdoa_for(),
            "sync_sigma_ns": sync_ns,
        }).json()["fix"]["sigma_m"]

    assert sigma(5.0) > sigma(0.1) * 5


def test_tdoa_rejects_two_anchors(client):
    response = client.post("/api/v1/agent/uwb/tdoa", json={
        "anchors": _TDOA_ANCHORS[:2], "tdoa_m": {"a1": 1.0}, "sync_sigma_ns": 0.1,
    })
    assert response.status_code == 400


def test_tdoa_requires_an_explicit_sync_figure(client):
    """Omitting sync must fail loudly rather than assume perfect clocks."""
    response = client.post("/api/v1/agent/uwb/tdoa", json={
        "anchors": _TDOA_ANCHORS, "tdoa_m": _tdoa_for(),
    })
    assert response.status_code == 422
