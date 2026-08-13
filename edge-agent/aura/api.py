"""FastAPI application: REST + WebSocket surface of the Aura edge agent."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from .audit import AuditStore, CausalValidator
from .config import CONFIG, AgentConfig
from .fusion import FusionPipeline
from .mapping import mesh_to_gltf
from .passive_radar import (
    compute_caf_fft,
    detect_targets,
    doppler_resolution,
    eca_cancel,
    generate_ofdm_reference,
    range_resolution,
    simulate_surveillance,
    velocity_resolution,
)
from .rti import RtiGrid, RtiProcessor
from .scenarios import SCENARIO_TYPES, ScenarioParams
from .storage import LocalVectorStore
from .voxel import LABEL_NAMES, VoxelWorld

LOGGER = logging.getLogger("aura.api")
API_VERSION = "1.0.0"


# ----------------------------------------------------------------------
# request models
# ----------------------------------------------------------------------
class ScenarioRequest(BaseModel):
    scenario: str = Field(default="evacuation", description=f"one of {SCENARIO_TYPES}")
    people: int = Field(default=24, ge=0, le=400)
    smoke_density: float = Field(default=0.35, ge=0.0, le=1.0)
    fire_source: list[float] | None = None
    speed: float = Field(default=1.0, gt=0.0, le=20.0)
    duration: float = Field(default=180.0, ge=5.0, le=3600.0)
    panic: float = Field(default=0.3, ge=0.0, le=1.0)
    exits: list[str] = Field(default_factory=list)
    seed: int = 1234
    notes: str = ""


class ConfigRequest(BaseModel):
    loop_hz: float | None = Field(default=None, gt=0.5, le=200.0)
    broadcast_hz: float | None = Field(default=None, gt=0.1, le=60.0)
    persist_every: int | None = Field(default=None, ge=1, le=1000)
    project: str | None = None
    retention_seconds: int | None = Field(default=None, ge=60)
    retention_records: int | None = Field(default=None, ge=100)
    sensors: dict[str, Any] | None = None


class TokenRequest(BaseModel):
    address: str
    label: str = "Token"
    x: float = 0.0
    y: float = 0.0


class IngestRequest(BaseModel):
    """Sensor payload uploaded by a secondary CT45P."""

    device_id: str
    timestamp: float | None = None
    rssi: dict[str, int] = Field(default_factory=dict)
    position: list[float] | None = None
    battery: float | None = None
    note: str = ""


class PauseRequest(BaseModel):
    paused: bool = True


class RtiNodeSpec(BaseModel):
    id: str
    x: float
    y: float


class RtiConfigRequest(BaseModel):
    nodes: list[RtiNodeSpec]
    min_x: float = 0.0
    min_y: float = 0.0
    max_x: float = 20.0
    max_y: float = 14.0
    resolution: float = Field(default=0.25, gt=0.02, le=2.0)
    alpha: float = Field(default=0.08, gt=0.0, le=10.0)
    use_l1: bool = True


class RtiMeasurementRequest(BaseModel):
    """RSSI per link, keyed as 'nodeA|nodeB'."""

    rssi: dict[str, float]
    calibrate: bool = False


class RadarRequest(BaseModel):
    sample_rate: float = Field(default=2.4e6, gt=1e3)
    samples: int = Field(default=32768, ge=1024, le=1 << 20)
    carrier_hz: float = Field(default=626e6, gt=1e6)
    eca_taps: int = Field(default=16, ge=0, le=128)
    max_range_bins: int = Field(default=48, ge=4, le=512)
    num_batches: int = Field(default=64, ge=4, le=512)
    threshold_db: float = Field(default=10.0, ge=0.0, le=60.0)
    targets: list[list[float]] = Field(default_factory=lambda: [[20, 80.0, 0.02], [45, -120.0, 0.01]])
    direct_path_gain: float = 1000.0


class AuditRequest(BaseModel):
    actor: str = "operator"
    action: str
    payload: dict[str, Any] = Field(default_factory=dict)
    severity: str = "info"


# ----------------------------------------------------------------------
# websocket hub
# ----------------------------------------------------------------------
class ConnectionHub:
    """Fan-out of telemetry frames to all connected WebSocket clients."""

    def __init__(self) -> None:
        self.connections: set[WebSocket] = set()
        self.loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[dict] | None = None
        self._task: asyncio.Task | None = None
        self.dropped = 0
        self.sent = 0

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(maxsize=64)
        self._task = asyncio.create_task(self._pump())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        for ws in list(self.connections):
            with contextlib.suppress(Exception):
                await ws.close()
        self.connections.clear()

    def publish_threadsafe(self, frame: dict) -> None:
        """Called from the fusion thread."""
        if not self.loop or not self._queue:
            return
        try:
            self.loop.call_soon_threadsafe(self._offer, frame)
        except RuntimeError:  # pragma: no cover - loop closed during shutdown
            pass

    def _offer(self, frame: dict) -> None:
        assert self._queue is not None
        if self._queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
                self.dropped += 1
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(frame)

    async def _pump(self) -> None:
        assert self._queue is not None
        while True:
            frame = await self._queue.get()
            if not self.connections:
                continue
            message = json.dumps(frame, default=_json_default)
            dead: list[WebSocket] = []
            for ws in list(self.connections):
                try:
                    await ws.send_text(message)
                    self.sent += 1
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self.connections.discard(ws)

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.connections.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self.connections.discard(ws)

    async def send(self, ws: WebSocket, payload: dict) -> None:
        await ws.send_text(json.dumps(payload, default=_json_default))


def _json_default(obj: Any) -> Any:
    try:
        import numpy as np

        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
    except Exception:  # pragma: no cover
        pass
    if isinstance(obj, set):
        return sorted(obj)
    return str(obj)


# ----------------------------------------------------------------------
# application factory
# ----------------------------------------------------------------------
def create_app(config: AgentConfig | None = None, autostart: bool = True) -> FastAPI:
    cfg = config or CONFIG
    store = LocalVectorStore(cfg.resolved_db_path(), cfg.project)
    pipeline = FusionPipeline(cfg, store)
    hub = ConnectionHub()
    audit = AuditStore(store.connection)
    voxels = VoxelWorld(voxel_size=0.10)
    rti_state: dict[str, Any] = {"processor": None}

    app = FastAPI(
        title="Aura Edge Agent",
        version=API_VERSION,
        description="Multisensor fusion, 3D reconstruction and scenario simulation for Honeywell CT45P.",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    origins = [o.strip() for o in cfg.cors_origins.split(",")] if cfg.cors_origins else ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.config = cfg
    app.state.pipeline = pipeline
    app.state.hub = hub
    app.state.audit = audit
    app.state.voxels = voxels
    app.state.started_at = time.time()

    def require_token(authorization: str | None = Header(default=None)) -> None:
        if not cfg.api_token:
            return
        expected = f"Bearer {cfg.api_token}"
        if authorization != expected:
            raise HTTPException(status_code=401, detail="invalid or missing bearer token")

    auth = [Depends(require_token)]

    # ------------------------------------------------------------------
    @app.on_event("startup")
    async def _startup() -> None:
        await hub.start()
        pipeline.subscribe(hub.publish_threadsafe)
        audit.append("agent", "agent.start", {"project": cfg.project, "simulate": cfg.simulate}, "security")
        if autostart:
            pipeline.start()
        LOGGER.info("Aura edge agent ready on %s:%s (simulate=%s)", cfg.host, cfg.port, cfg.simulate)

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        audit.append("agent", "agent.stop", severity="security")
        pipeline.stop()
        await hub.stop()
        store.close()

    # ------------------------------------------------------------------
    # health & meta
    # ------------------------------------------------------------------
    @app.get("/health", tags=["meta"])
    def health() -> dict:
        sensors = {d.name: d.info for d in pipeline.drivers}
        healthy = all(s["healthy"] for s in sensors.values()) if sensors else False
        return {
            "status": "ok" if healthy else "degraded",
            "version": API_VERSION,
            "uptime": round(time.time() - app.state.started_at, 1),
            "simulate": cfg.simulate,
            "iterations": pipeline.iterations,
            "clients": len(hub.connections),
            "sensors": {k: v["healthy"] for k, v in sensors.items()},
        }

    @app.get("/api/v1/agent/info", tags=["meta"])
    def info() -> dict:
        return {
            "name": "Aura Edge Agent",
            "version": API_VERSION,
            "scenarios": list(SCENARIO_TYPES),
            "endpoints": [r.path for r in app.routes if getattr(r, "path", "").startswith("/api")],
            "websocket": "/ws/agent/events",
            "auth_required": bool(cfg.api_token),
        }

    # ------------------------------------------------------------------
    # state / history
    # ------------------------------------------------------------------
    @app.get("/api/v1/agent/state", tags=["state"], dependencies=auth)
    def get_state() -> dict:
        return pipeline.state()

    @app.get("/api/v1/agent/telemetry", tags=["state"], dependencies=auth)
    def get_telemetry() -> dict:
        return pipeline.telemetry or {"type": "telemetry", "timestamp": time.time(), "warming_up": True}

    @app.get("/api/v1/agent/history", tags=["state"], dependencies=auth)
    def get_history(
        limit: int = Query(default=500, ge=1, le=10000),
        since: float | None = Query(default=None),
    ) -> dict:
        records = pipeline.history(limit=limit, since=since)
        return {"count": len(records), "records": records}

    @app.get("/api/v1/agent/events", tags=["state"], dependencies=auth)
    def get_events(source: str | None = None, limit: int = Query(default=200, ge=1, le=5000)) -> dict:
        return {"events": store.events(source=source, limit=limit)}

    # ------------------------------------------------------------------
    # map
    # ------------------------------------------------------------------
    @app.get("/api/v1/agent/map", tags=["map"], dependencies=auth)
    def get_map(max_points: int = Query(default=4000, ge=100, le=40000)) -> dict:
        return pipeline.map_payload(max_points=max_points)

    @app.post("/api/v1/agent/map/save", tags=["map"], dependencies=auth)
    def post_map_save() -> dict:
        return pipeline.save_map()

    @app.get("/api/v1/agent/map/versions", tags=["map"], dependencies=auth)
    def get_map_versions() -> dict:
        return {"versions": store.map_versions()}

    @app.post("/api/v1/agent/map/load", tags=["map"], dependencies=auth)
    def post_map_load(version: int | None = None) -> dict:
        ok = pipeline.load_map(version)
        return {"loaded": ok, "version": version}

    @app.get("/api/v1/agent/mesh", tags=["map"], dependencies=auth)
    def get_mesh(height: float = Query(default=2.7, gt=0.5, le=10.0)) -> dict:
        return pipeline.mesh(height=height)

    @app.get("/api/v1/agent/export/gltf", tags=["map"], dependencies=auth)
    def get_gltf(height: float = Query(default=2.7, gt=0.5, le=10.0)) -> Response:
        gltf = mesh_to_gltf(pipeline.mesh(height=height), name=cfg.project)
        return Response(
            content=json.dumps(gltf),
            media_type="model/gltf+json",
            headers={"Content-Disposition": f'attachment; filename="{cfg.project}.gltf"'},
        )

    @app.get("/api/v1/agent/export/json", tags=["map"], dependencies=auth)
    def get_export_json(limit: int = Query(default=2000, ge=1, le=20000)) -> Response:
        payload = {
            "exported_at": time.time(),
            "project": cfg.project,
            "state": pipeline.state(),
            "map": pipeline.map_payload(max_points=20000),
            "history": pipeline.history(limit=limit),
            "scenarios": store.scenario_runs(limit=25),
        }
        return Response(
            content=json.dumps(payload, default=_json_default),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{cfg.project}-export.json"'},
        )

    # ------------------------------------------------------------------
    # scenarios
    # ------------------------------------------------------------------
    @app.post("/api/v1/agent/scenario/start", tags=["scenario"], dependencies=auth)
    def post_scenario_start(request: ScenarioRequest) -> dict:
        if request.scenario not in SCENARIO_TYPES:
            raise HTTPException(status_code=400, detail=f"unknown scenario '{request.scenario}'")
        params = ScenarioParams.from_dict(request.model_dump())
        return pipeline.start_scenario(params)

    @app.post("/api/v1/agent/scenario/stop", tags=["scenario"], dependencies=auth)
    def post_scenario_stop() -> dict:
        metrics = pipeline.stop_scenario()
        if metrics is None:
            raise HTTPException(status_code=409, detail="no scenario is running")
        return metrics

    @app.post("/api/v1/agent/scenario/pause", tags=["scenario"], dependencies=auth)
    def post_scenario_pause(request: PauseRequest) -> dict:
        ok = pipeline.scenarios.pause(request.paused)
        if not ok:
            raise HTTPException(status_code=409, detail="no scenario is running")
        return {"paused": request.paused}

    @app.get("/api/v1/agent/scenario", tags=["scenario"], dependencies=auth)
    def get_scenario() -> dict:
        snapshot = pipeline.scenarios.snapshot()
        return {"active": snapshot, "history": store.scenario_runs(limit=20)}

    # ------------------------------------------------------------------
    # config / tokens / projects
    # ------------------------------------------------------------------
    @app.post("/api/v1/agent/config", tags=["config"], dependencies=auth)
    def post_config(request: ConfigRequest) -> dict:
        patch = {k: v for k, v in request.model_dump().items() if v is not None}
        changed = pipeline.apply_config(patch)
        return {"changed": changed, "config": cfg.as_dict()}

    @app.get("/api/v1/agent/config", tags=["config"], dependencies=auth)
    def get_config() -> dict:
        return cfg.as_dict()

    @app.get("/api/v1/agent/tokens", tags=["config"], dependencies=auth)
    def get_tokens() -> dict:
        return {
            "tokens": [
                {"address": addr, "label": label, "x": x, "y": y}
                for addr, (label, x, y) in pipeline.ble.tokens.items()
            ]
        }

    @app.post("/api/v1/agent/tokens", tags=["config"], dependencies=auth)
    def post_token(request: TokenRequest) -> dict:
        pipeline.ble.add_token(request.address, request.label, request.x, request.y)
        store.save_event("ble_token", {"action": "add", **request.model_dump()})
        return {"added": request.address, "count": len(pipeline.ble.tokens)}

    @app.delete("/api/v1/agent/tokens/{address}", tags=["config"], dependencies=auth)
    def delete_token(address: str) -> dict:
        removed = pipeline.ble.remove_token(address)
        if not removed:
            raise HTTPException(status_code=404, detail="token not found")
        return {"removed": address}

    @app.get("/api/v1/agent/projects", tags=["config"], dependencies=auth)
    def get_projects() -> dict:
        return {"projects": store.list_projects(), "active": cfg.project}

    # ------------------------------------------------------------------
    # multi-device ingest
    # ------------------------------------------------------------------
    @app.post("/api/v1/agent/ingest", tags=["mesh"], dependencies=auth)
    def post_ingest(request: IngestRequest) -> dict:
        payload = request.model_dump()
        payload["received_at"] = time.time()
        store.save_event(f"device:{request.device_id}", payload)
        hub.publish_threadsafe({"type": "device_update", "device": request.device_id, "payload": payload})
        return {"accepted": True, "device_id": request.device_id}

    @app.get("/api/v1/agent/devices", tags=["mesh"], dependencies=auth)
    def get_devices(limit: int = Query(default=50, ge=1, le=500)) -> dict:
        events = store.events(limit=limit * 4)
        devices: dict[str, dict] = {}
        for event in events:
            if not event["source"].startswith("device:"):
                continue
            device_id = event["source"].split(":", 1)[1]
            if device_id not in devices:
                devices[device_id] = {"device_id": device_id, "last_seen": event["timestamp"], **event["payload"]}
        return {"devices": list(devices.values())[:limit]}


    # ------------------------------------------------------------------
    # AURA 6.0: radio tomographic imaging
    # ------------------------------------------------------------------
    @app.post("/api/v1/agent/rti/configure", tags=["rti"], dependencies=auth)
    def post_rti_configure(request: RtiConfigRequest) -> dict:
        if len(request.nodes) < 3:
            raise HTTPException(status_code=400, detail="RTI needs at least 3 nodes")
        grid = RtiGrid(request.min_x, request.min_y, request.max_x, request.max_y, request.resolution)
        if grid.voxel_count > 20000:
            raise HTTPException(
                status_code=400,
                detail=f"grid too large ({grid.voxel_count} voxels); increase the resolution",
            )
        processor = RtiProcessor(
            grid,
            {node.id: (node.x, node.y) for node in request.nodes},
            alpha=request.alpha,
            use_l1=request.use_l1,
        )
        rti_state["processor"] = processor
        audit.append("rti", "rti.configure", {"nodes": len(request.nodes), "voxels": grid.voxel_count}, "notice")
        return {
            "configured": True,
            "nodes": len(request.nodes),
            "links": len(processor.links),
            "voxels": grid.voxel_count,
            "grid": grid.as_dict(),
            "lipschitz": round(processor.lipschitz, 4),
        }

    def _parse_links(rssi: dict[str, float]) -> dict[tuple[str, str], float]:
        parsed: dict[tuple[str, str], float] = {}
        for key, value in rssi.items():
            if "|" not in key:
                continue
            a, b = key.split("|", 1)
            parsed[(a.strip(), b.strip())] = float(value)
        return parsed

    @app.post("/api/v1/agent/rti/measure", tags=["rti"], dependencies=auth)
    def post_rti_measure(request: RtiMeasurementRequest) -> dict:
        processor: RtiProcessor | None = rti_state.get("processor")
        if processor is None:
            raise HTTPException(status_code=409, detail="call /rti/configure first")
        links = _parse_links(request.rssi)
        if not links:
            raise HTTPException(status_code=400, detail="no valid 'nodeA|nodeB' keys in rssi")
        if request.calibrate:
            samples = processor.calibrate(links)
            return {"calibrated": True, "samples": samples}
        if not processor.calibrated:
            raise HTTPException(status_code=409, detail="calibrate on an empty room first")
        result = processor.update(links)
        payload = processor.as_dict()
        payload["iterations"] = result.iterations
        payload["converged"] = result.converged
        hub.publish_threadsafe({"type": "rti", "payload": payload})
        return payload

    @app.get("/api/v1/agent/rti", tags=["rti"], dependencies=auth)
    def get_rti() -> dict:
        processor: RtiProcessor | None = rti_state.get("processor")
        if processor is None:
            return {"configured": False}
        return {"configured": True, **processor.as_dict()}

    # ------------------------------------------------------------------
    # AURA 6.0: passive radar
    # ------------------------------------------------------------------
    @app.post("/api/v1/agent/radar/process", tags=["radar"], dependencies=auth)
    def post_radar_process(request: RadarRequest) -> dict:
        """Run the CAF pipeline.

        With no SDR attached the reference/surveillance pair is synthesised
        from the requested target list, which keeps the endpoint exercisable
        (and testable) without hardware.
        """
        import numpy as np

        rng = np.random.default_rng(0)
        reference = generate_ofdm_reference(request.samples, request.sample_rate, rng=rng)
        targets = [(int(t[0]), float(t[1]), float(t[2])) for t in request.targets if len(t) >= 3]
        surveillance = simulate_surveillance(
            reference, request.sample_rate, targets,
            direct_path_gain=request.direct_path_gain, noise_sigma=0.005, rng=rng,
        )
        clean = eca_cancel(surveillance, reference, num_taps=request.eca_taps) if request.eca_taps else surveillance
        rd_map = compute_caf_fft(
            clean, reference, request.sample_rate,
            max_range_bins=request.max_range_bins,
            num_batches=request.num_batches,
            carrier_hz=request.carrier_hz,
        )
        detections = detect_targets(rd_map, threshold_db=request.threshold_db)
        return {
            "detections": [d.as_dict() for d in detections],
            "map": rd_map.as_dict(decimate=2),
            "resolution": {
                "range_m": round(range_resolution(request.sample_rate), 2),
                "velocity_ms": round(velocity_resolution(request.carrier_hz, rd_map.integration_time), 4),
                "doppler_hz": round(doppler_resolution(rd_map.integration_time), 2),
                "integration_time_s": round(rd_map.integration_time, 6),
            },
        }

    @app.get("/api/v1/agent/radar/limits", tags=["radar"], dependencies=auth)
    def get_radar_limits(bandwidth_hz: float = Query(default=2.4e6, gt=1e3),
                         carrier_hz: float = Query(default=626e6, gt=1e6),
                         integration_time: float = Query(default=0.1, gt=0.0)) -> dict:
        """Physical limits for a given SDR configuration - no processing beats these."""
        return {
            "bandwidth_hz": bandwidth_hz,
            "range_resolution_m": round(range_resolution(bandwidth_hz), 2),
            "velocity_resolution_ms": round(velocity_resolution(carrier_hz, integration_time), 4),
            "doppler_resolution_hz": round(doppler_resolution(integration_time), 3),
            "note": "range resolution is c/(2B); an RTL-SDR's 2.4 MHz gives ~62 m, a full 8 MHz DVB-T channel ~19 m",
        }

    # ------------------------------------------------------------------
    # AURA 6.0: voxels
    # ------------------------------------------------------------------
    @app.get("/api/v1/agent/voxels", tags=["voxel"], dependencies=auth)
    def get_voxels(threshold: int = Query(default=1000, ge=0, le=65535),
                   max_points: int = Query(default=20000, ge=100, le=200000)) -> dict:
        return {
            "stats": voxels.stats(),
            "labels": LABEL_NAMES,
            "points": voxels.point_cloud(threshold=threshold, max_points=max_points),
        }

    @app.post("/api/v1/agent/voxels/ingest", tags=["voxel"], dependencies=auth)
    def post_voxels_ingest(points: list[list[float]], label: int = Query(default=1, ge=0, le=5)) -> dict:
        count = voxels.integrate_points(points, label=label, timestamp=time.time())
        return {"ingested": count, "stats": voxels.stats()}

    # ------------------------------------------------------------------
    # AURA 6.0: audit chain
    # ------------------------------------------------------------------
    @app.get("/api/v1/agent/audit", tags=["audit"], dependencies=auth)
    def get_audit(limit: int = Query(default=50, ge=1, le=1000), severity: str | None = None) -> dict:
        return {"stats": audit.stats(), "entries": audit.tail(limit, severity)}

    @app.post("/api/v1/agent/audit", tags=["audit"], dependencies=auth)
    def post_audit(request: AuditRequest) -> dict:
        entry = audit.append(request.actor, request.action, request.payload, request.severity)
        return entry.as_dict()

    @app.get("/api/v1/agent/audit/verify", tags=["audit"], dependencies=auth)
    def get_audit_verify() -> dict:
        ok, index, message = audit.verify()
        return {"valid": ok, "first_bad_index": index, "message": message, "head": audit.validator.last_hash}

    # ------------------------------------------------------------------
    # websocket
    # ------------------------------------------------------------------
    @app.websocket("/ws/agent/events")
    async def ws_events(websocket: WebSocket) -> None:
        if cfg.api_token:
            token = websocket.query_params.get("token")
            if token != cfg.api_token:
                await websocket.close(code=4401)
                return
        await hub.connect(websocket)
        try:
            await hub.send(
                websocket,
                {
                    "type": "hello",
                    "version": API_VERSION,
                    "timestamp": time.time(),
                    "world": pipeline.world.as_dict(),
                    "config": {"loop_hz": cfg.loop_hz, "broadcast_hz": cfg.broadcast_hz},
                },
            )
            if pipeline.telemetry:
                await hub.send(websocket, pipeline.telemetry)
            while True:
                raw = await websocket.receive_text()
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    await hub.send(websocket, {"type": "error", "message": "invalid JSON"})
                    continue
                await _handle_ws_command(websocket, message, pipeline, hub)
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # pragma: no cover - transport errors
            LOGGER.debug("websocket error: %s", exc)
        finally:
            hub.disconnect(websocket)

    @app.exception_handler(Exception)
    async def _unhandled(request, exc):  # pragma: no cover - safety net
        LOGGER.exception("unhandled error on %s", request.url.path)
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    return app


async def _handle_ws_command(ws: WebSocket, message: dict, pipeline: FusionPipeline, hub: ConnectionHub) -> None:
    """Command channel shared by the web UI and the Android client."""
    command = str(message.get("command", "")).lower()
    payload = message.get("payload") or {}

    if command == "ping":
        await hub.send(ws, {"type": "pong", "timestamp": time.time()})
    elif command == "state":
        await hub.send(ws, {"type": "state", "payload": pipeline.state()})
    elif command == "map":
        await hub.send(ws, {"type": "map", "payload": pipeline.map_payload(max_points=int(payload.get("max_points", 4000)))})
    elif command == "history":
        await hub.send(ws, {"type": "history", "payload": pipeline.history(limit=int(payload.get("limit", 500)))})
    elif command == "scenario.start":
        params = ScenarioParams.from_dict(payload)
        await hub.send(ws, {"type": "scenario", "payload": pipeline.start_scenario(params)})
    elif command == "scenario.stop":
        await hub.send(ws, {"type": "scenario_metrics", "payload": pipeline.stop_scenario()})
    elif command == "scenario.pause":
        pipeline.scenarios.pause(bool(payload.get("paused", True)))
        await hub.send(ws, {"type": "ack", "command": command})
    elif command == "config":
        changed = pipeline.apply_config(payload)
        await hub.send(ws, {"type": "config", "changed": changed, "payload": pipeline.config.as_dict()})
    elif command == "ingest":
        pipeline.store.save_event(f"device:{payload.get('device_id', 'unknown')}", payload)
        await hub.send(ws, {"type": "ack", "command": command})
    else:
        await hub.send(ws, {"type": "error", "message": f"unknown command '{command}'"})


app = create_app()
