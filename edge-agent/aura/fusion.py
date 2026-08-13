"""The fusion pipeline: the heart of the edge agent.

One control loop at ``loop_hz``:

1. read IMU  -> EKF ``predict``
2. read LiDAR -> scan-match against the occupancy grid -> EKF pose update
3. read UWB   -> range updates + micro-Doppler vitals (through-wall)
4. read BLE   -> RSSI multilateration -> weak position update
5. read mmWave/thermal -> people detection, fused into tracks
6. integrate the LiDAR sweep into the occupancy grid
7. persist a transform record every ``persist_every`` iterations
8. publish a telemetry frame to all WebSocket subscribers
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

from .config import AgentConfig
from .doppler import MicroDopplerAnalyzer, ThroughWallTracker
from .ekf import ExtendedKalmanFilter, EkfConfig, FusionDiagnostics, wrap_pi
from .mapping import OccupancyGrid, build_mesh, extract_walls
from .scenarios import ScenarioEngine, ScenarioParams
from .sensors import BleDriver, ImuDriver, LidarDriver, MmwaveDriver, ThermalDriver, UwbDriver
from .sensors.imu import StaticDetector
from .storage import LocalVectorStore
from .world import WORLD


@dataclass
class PersonTrack:
    """A fused person track (mmWave + thermal + UWB)."""

    track_id: str
    x: float
    y: float
    vx: float = 0.0
    vy: float = 0.0
    confidence: float = 0.3
    sources: set[str] = field(default_factory=set)
    hits: int = 0
    last_seen: float = 0.0
    behind_wall: bool = False

    def as_dict(self) -> dict:
        return {
            "track_id": self.track_id,
            "x": round(self.x, 3),
            "y": round(self.y, 3),
            "vx": round(self.vx, 3),
            "vy": round(self.vy, 3),
            "speed": round(math.hypot(self.vx, self.vy), 3),
            "confidence": round(min(1.0, self.confidence), 3),
            "sources": sorted(self.sources),
            "behind_wall": self.behind_wall,
            "age": round(time.time() - self.last_seen, 2),
        }


class PersonTracker:
    """Nearest-neighbour multi-target tracker with exponential smoothing."""

    def __init__(self, gate: float = 1.2, max_age: float = 3.0) -> None:
        self.gate = gate
        self.max_age = max_age
        self.tracks: dict[str, PersonTrack] = {}
        self._next = 1

    def update(self, detections: list[tuple[float, float, str, bool]], now: float) -> list[PersonTrack]:
        for x, y, source, behind in detections:
            best, best_d = None, self.gate
            for track in self.tracks.values():
                d = math.hypot(track.x - x, track.y - y)
                if d < best_d:
                    best, best_d = track, d
            if best is None:
                tid = f"p-{self._next:03d}"
                self._next += 1
                best = PersonTrack(track_id=tid, x=x, y=y, last_seen=now)
                self.tracks[tid] = best
            dt = max(1e-2, now - best.last_seen)
            nvx = (x - best.x) / dt
            nvy = (y - best.y) / dt
            best.vx = 0.7 * best.vx + 0.3 * float(np.clip(nvx, -3.0, 3.0))
            best.vy = 0.7 * best.vy + 0.3 * float(np.clip(nvy, -3.0, 3.0))
            best.x = 0.55 * best.x + 0.45 * x
            best.y = 0.55 * best.y + 0.45 * y
            best.sources.add(source)
            best.hits += 1
            best.behind_wall = behind
            best.confidence = min(1.0, 0.18 * best.hits + 0.2 * len(best.sources))
            best.last_seen = now
        for tid in [t for t, tr in self.tracks.items() if now - tr.last_seen > self.max_age]:
            del self.tracks[tid]
        return [t for t in self.tracks.values() if t.hits >= 2]


class ScanMatcher:
    """Lightweight 3-DOF scan matcher (grid-search correlation on the map).

    Good enough to bound IMU drift without pulling in a full ICP/g2o stack,
    and cheap enough for a handheld device.
    """

    def __init__(self, grid: OccupancyGrid) -> None:
        self.grid = grid
        self.enabled = True
        self.last_score = 0.0

    def match(self, guess: tuple[float, float, float], scan_body: np.ndarray) -> tuple[float, float, float, float] | None:
        """Returns ``(x, y, yaw, score)`` or ``None`` when the map is too sparse."""
        if not self.enabled or len(scan_body) < 30 or self.grid.updates < 8:
            return None
        prob = self.grid.probability()
        if float(prob.max()) < 0.6:
            return None
        gx, gy, gyaw = guess
        best = (gx, gy, gyaw, -1.0)
        # coarse-to-fine search
        for (span_xy, step_xy, span_yaw, step_yaw) in ((0.30, 0.10, 0.06, 0.03), (0.10, 0.05, 0.02, 0.01)):
            cx, cy, cyaw, _ = best
            for dyaw in np.arange(-span_yaw, span_yaw + 1e-9, step_yaw):
                yaw = cyaw + dyaw
                ca, sa = math.cos(yaw), math.sin(yaw)
                rx = scan_body[:, 0] * ca - scan_body[:, 1] * sa
                ry = scan_body[:, 0] * sa + scan_body[:, 1] * ca
                for dx in np.arange(-span_xy, span_xy + 1e-9, step_xy):
                    for dy in np.arange(-span_xy, span_xy + 1e-9, step_xy):
                        px = rx + cx + dx
                        py = ry + cy + dy
                        ix = ((px - self.grid.origin[0]) / self.grid.resolution).astype(int)
                        iy = ((py - self.grid.origin[1]) / self.grid.resolution).astype(int)
                        valid = (ix >= 0) & (ix < self.grid.cells) & (iy >= 0) & (iy < self.grid.cells)
                        if not np.any(valid):
                            continue
                        score = float(prob[iy[valid], ix[valid]].sum()) / max(1, len(scan_body))
                        if score > best[3]:
                            best = (cx + dx, cy + dy, yaw, score)
        self.last_score = best[3]
        if best[3] < 0.12:
            return None
        return best


class FusionPipeline:
    """Owns all drivers, the EKF, the map, the scenario engine and telemetry."""

    def __init__(self, config: AgentConfig, store: LocalVectorStore | None = None) -> None:
        self.config = config
        self.store = store or LocalVectorStore(config.resolved_db_path(), config.project)
        self.world = WORLD
        self.ekf = ExtendedKalmanFilter(EkfConfig())
        self.grid = OccupancyGrid(size_m=config.map_size, resolution=config.map_resolution)
        self.matcher = ScanMatcher(self.grid)
        self.doppler = MicroDopplerAnalyzer()
        self.through_wall = ThroughWallTracker()
        self.people = PersonTracker()
        self.static_detector = StaticDetector()
        self.scenarios = ScenarioEngine(self.world)
        self.diagnostics = FusionDiagnostics()

        sc = config.sensors
        sim = config.simulate
        self.lidar = LidarDriver(sc.lidar_port, sc.lidar_baud, sim, self.world)
        self.mmwave = MmwaveDriver(sc.mmwave_port, sc.mmwave_baud, sim, sc.mmwave_reduced, self.world)
        self.uwb = UwbDriver(sc.uwb_port, sc.uwb_baud, sim, self.world)
        self.ble = BleDriver(sc.ble_adapter, sim, self.world)
        self.imu = ImuDriver(sc.imu_port, 115200, sim, self.world)
        self.thermal = ThermalDriver("", sim, self.world)
        self.drivers = [self.lidar, self.mmwave, self.uwb, self.ble, self.imu, self.thermal]

        self._subscribers: list[Callable[[dict], None]] = []
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._running = threading.Event()
        self._iterations = 0
        self._last_loop = time.time()
        self._last_broadcast = 0.0
        self._last_scan = None
        self._loop_times: list[float] = []
        self._started_at = time.time()
        self.telemetry: dict[str, Any] = {}
        self.battery = 100.0
        self.temperature = 33.0
        self.walls: list[dict] = []
        self._last_wall_extraction = 0.0
        self._active_run_id: str | None = None
        self._pose_initialized = False
        self._init_fixes = 0
        self.device_height = 1.4

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        for driver in self.drivers:
            driver.open()
        self._running.set()
        self._thread = threading.Thread(target=self._loop, name="aura-fusion", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running.clear()
        if self._thread:
            self._thread.join(timeout=3.0)
        for driver in self.drivers:
            driver.close()

    def subscribe(self, callback: Callable[[dict], None]) -> Callable[[], None]:
        with self._lock:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return unsubscribe

    def _publish(self, frame: dict) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for cb in subscribers:
            try:
                cb(frame)
            except Exception:  # pragma: no cover - a dead subscriber must not kill the loop
                pass

    # ------------------------------------------------------------------
    # the loop
    # ------------------------------------------------------------------
    def _loop(self) -> None:
        while self._running.is_set():
            begin = time.time()
            try:
                self.tick()
            except Exception as exc:  # pragma: no cover - defensive
                self.diagnostics.rejected_updates += 1
                self.telemetry["error"] = f"{type(exc).__name__}: {exc}"
            elapsed = time.time() - begin
            self._loop_times.append(elapsed)
            if len(self._loop_times) > 200:
                del self._loop_times[:-200]
            period = 1.0 / max(1.0, self.config.loop_hz)
            time.sleep(max(0.0, period - elapsed))

    def _bootstrap_pose(self, uwb_reading) -> None:
        """Anchor the filter to the map frame before any mapping starts.

        Without this the EKF would start at the origin, the scan matcher
        would lock onto a self-consistently shifted map and the whole
        reconstruction would be offset from the anchor/token coordinates.
        We therefore require a few consistent UWB trilaterations first.
        """
        los_ranges = {k: v for k, v in uwb_reading.ranges.items() if uwb_reading.los.get(k, True)}
        fix = self.uwb.multilaterate(los_ranges if len(los_ranges) >= 3 else uwb_reading.ranges)
        if fix is None:
            # No UWB infrastructure: fall back to BLE token multilateration.
            beacons = self.ble.read()
            ble_fix = self.ble.multilaterate(beacons) if beacons else None
            if ble_fix is None:
                return
            fix = (ble_fix[0], ble_fix[1])
        self._init_fixes += 1
        alpha = 1.0 / self._init_fixes
        px, py = self.ekf.position[0], self.ekf.position[1]
        if self._init_fixes == 1:
            px, py = fix
        else:
            px = (1 - alpha) * px + alpha * fix[0]
            py = (1 - alpha) * py + alpha * fix[1]
        self.ekf.x[0] = px
        self.ekf.x[1] = py
        self.ekf.x[2] = self.device_height
        if self._init_fixes >= 4:
            self._pose_initialized = True
            self.diagnostics.note("pose_bootstrap")

    def tick(self) -> dict:
        """One full fusion iteration (also callable synchronously in tests)."""
        now = time.time()
        dt = min(max(now - self._last_loop, 1e-3), 0.5)
        self._last_loop = now
        self._iterations += 1

        # --- scenario -------------------------------------------------
        scenario_snapshot = self.scenarios.step(dt)
        people_gt = self.scenarios.people()

        # --- 1. IMU + prediction -------------------------------------
        imu_sample = self.imu.read()
        if imu_sample is not None:
            self.ekf.predict(imu_sample.gyro, imu_sample.accel, dt)
            self.diagnostics.note("imu")
            if self.static_detector.push(imu_sample):
                self.ekf.update_zero_velocity()
                self.diagnostics.note("zupt")
            self.ekf.update_yaw(imu_sample.heading(), sigma=0.35)
            self.temperature = 0.98 * self.temperature + 0.02 * imu_sample.temperature
        else:
            self.diagnostics.dropped_samples += 1

        # ground truth pose for the simulators
        truth = self.imu.ground_truth()
        if truth is not None:
            for driver in (self.lidar, self.mmwave, self.uwb, self.ble, self.thermal):
                driver.set_pose(*truth)
        self.mmwave.set_people(people_gt)
        self.thermal.set_people(people_gt)
        if self.scenarios.active and self.scenarios.active.fire_source:
            fx, fy = self.scenarios.active.fire_source
            self.thermal.set_fire((fx, fy, 0.6 + 0.4 * self.scenarios.active.params.smoke_density))
        else:
            self.thermal.set_fire(None)
        self.uwb.set_hidden_subjects(
            [(float(p[0]), float(p[1]), 0.28, 1.15) for p in people_gt[:6]]
        )

        # --- 2. UWB (read early: it anchors the map frame) ------------
        uwb_reading = self.uwb.read()
        if uwb_reading is not None and not self._pose_initialized:
            self._bootstrap_pose(uwb_reading)

        state = self.ekf.snapshot(now)
        pose = (state.position[0], state.position[1], state.orientation_euler[2])

        # --- 3. LiDAR -------------------------------------------------
        lidar_payload = None
        scan = self.lidar.read() if self._pose_initialized else None
        if scan is not None and len(scan):
            body = np.column_stack(
                [np.cos(scan.angles) * np.asarray(scan.distances),
                 np.sin(scan.angles) * np.asarray(scan.distances)]
            )
            match = self.matcher.match(pose, body)
            if match is not None:
                mx, my, myaw, score = match
                self.ekf.update_lidar_pose((mx, my), myaw)
                self.diagnostics.note("lidar_scanmatch")
                pose = (mx, my, myaw)
            points = scan.to_cartesian(pose)
            self.grid.integrate_scan((pose[0], pose[1]), points)
            self._last_scan = scan
            lidar_payload = scan.as_dict(decimate=2)
            lidar_payload["match_score"] = round(self.matcher.last_score, 4)

        # --- UWB fusion (the sample was read above) -------------------
        uwb_payload = None
        vitals = self.doppler.last
        detections: list[tuple[float, float, str, bool]] = []
        if uwb_reading is not None:
            # Fuse *every* anchor, but inflate the noise for non-line-of-sight
            # links instead of dropping them: with only 1-2 LOS anchors the
            # range-only geometry is underconstrained and the estimate slides
            # along the unobservable direction.
            for anchor, distance in uwb_reading.ranges.items():
                if anchor not in self.uwb.anchors:
                    continue
                los = uwb_reading.los.get(anchor, True)
                sigma = self.ekf.config.sigma_uwb_range if los else 0.55
                self.ekf.update_uwb_range(self.uwb.anchors[anchor], distance, sigma=sigma)
                self.diagnostics.note("uwb_range" if los else "uwb_range_nlos")
            # The operator carries the device at a roughly constant height;
            # this weak prior keeps the vertical channel observable.
            self.ekf.update_altitude(self.device_height, sigma=0.45)
            self.doppler.push(uwb_reading.timestamp, uwb_reading.cir_amplitude, uwb_reading.cir_phase)
            if self._iterations % 5 == 0:
                vitals = self.doppler.analyze()
            uwb_payload = uwb_reading.as_dict()
            uwb_payload["vitals"] = vitals.as_dict()

        # --- 4. BLE ---------------------------------------------------
        beacons = self.ble.read()
        ble_payload = None
        if beacons:
            fix = self.ble.multilaterate(beacons)
            if fix is not None:
                # RSSI trilateration is noisy and occasionally wild -> gate it
                # against the current estimate before letting it move the filter.
                offset = math.hypot(fix[0] - self.ekf.position[0], fix[1] - self.ekf.position[1])
                if offset <= 3.0 * max(1.0, fix[2]):
                    self.ekf.update_ble_position((fix[0], fix[1], self.ekf.position[2]), sigma=max(1.5, fix[2]))
                    self.diagnostics.note("ble_position")
                else:
                    self.diagnostics.rejected_updates += 1
            ble_payload = {
                "beacons": [b.as_dict() for b in beacons],
                "rssi": {b.address: b.rssi for b in beacons},
                "fix": {"x": round(fix[0], 3), "y": round(fix[1], 3), "sigma": round(fix[2], 3)} if fix else None,
            }

        # --- 5. mmWave + thermal -> people ---------------------------
        targets = self.mmwave.read()
        mmwave_payload = None
        if targets:
            px, py, yaw = pose
            moving = [t for t in targets if abs(t.velocity) > 0.18 and t.track_id >= 0]
            for target in moving:
                gx = px + target.x * math.cos(yaw) - target.y * math.sin(yaw)
                gy = py + target.x * math.sin(yaw) + target.y * math.cos(yaw)
                behind = not self.world.visible((px, py), (gx, gy))
                detections.append((gx, gy, "mmwave", behind))
            mmwave_payload = {
                "targets": [t.as_dict() for t in targets[:64]],
                "moving": len(moving),
                "profile": "reduced" if self.mmwave.reduced else "full",
            }

        thermal_frame = self.thermal.read() if self._iterations % 2 == 0 else None
        thermal_payload = None
        if thermal_frame is not None:
            px, py, yaw = pose
            spots = thermal_frame.hotspots(threshold=29.0)
            for spot in spots:
                bearing = yaw + spot["bearing"]
                est_range = float(np.clip(14.0 / max(1.0, spot["pixels"]) * 6.0, 1.0, 9.0))
                gx = px + est_range * math.cos(bearing)
                gy = py + est_range * math.sin(bearing)
                detections.append((gx, gy, "thermal", False))
            thermal_payload = thermal_frame.as_dict()

        # through-wall subjects from UWB vitals + occluded mmWave hits
        occluded = [(d[0], d[1], True) for d in detections if d[3]]
        twd = self.through_wall.update(occluded, vitals, now)
        for det in twd:
            detections.append((det.x, det.y, "uwb", True))

        tracks = self.people.update(detections, now)

        # --- 6. persistence ------------------------------------------
        state = self.ekf.snapshot(now)
        if self._iterations % max(1, self.config.persist_every) == 0:
            self.store.save_transform(
                self.ekf.transform(),
                covariance=state.covariance_diagonal[:9],
                velocity=state.velocity,
                metadata={
                    "iteration": self._iterations,
                    "match_score": round(self.matcher.last_score, 4),
                    "people": len(tracks),
                    "vitals": vitals.as_dict() if vitals.presence else None,
                    "scenario": self.scenarios.active.run_id if self.scenarios.active else None,
                },
                timestamp=now,
            )
        if self._iterations % 200 == 0:
            self.store.enforce_retention(self.config.retention_seconds, self.config.retention_records)

        # --- wall extraction (expensive -> throttled) -----------------
        if now - self._last_wall_extraction > 4.0 and self.grid.updates > 10:
            cells = self.grid.occupied_cells()
            if len(cells) > 40:
                self.walls = [s.as_dict() for s in extract_walls(cells)]
            self._last_wall_extraction = now

        # --- battery / thermal model ---------------------------------
        drain = 0.0009 * (1.0 + (0.6 if not self.mmwave.reduced else 0.2))
        self.battery = max(0.0, self.battery - drain)

        # --- 7. telemetry --------------------------------------------
        frame = {
            "type": "telemetry",
            "timestamp": now,
            "iteration": self._iterations,
            "ekf": state.as_dict(),
            "transform": self.ekf.transform(),
            "pose": {"x": pose[0], "y": pose[1], "yaw": pose[2]},
            "lidar": lidar_payload,
            "uwb": uwb_payload,
            "ble": ble_payload,
            "mmwave": mmwave_payload,
            "thermal": thermal_payload,
            "people": [t.as_dict() for t in tracks],
            "through_wall": [d.as_dict() for d in twd],
            "vitals": vitals.as_dict(),
            "map": {
                "stats": self.grid.stats().as_dict(),
                "walls": self.walls[:40],
            },
            "scenario": scenario_snapshot,
            "device": {
                "battery": round(self.battery, 2),
                "temperature": round(self.temperature, 2),
                "loop_hz": round(1.0 / max(1e-6, float(np.mean(self._loop_times))) if self._loop_times else 0.0, 1),
                "uptime": round(now - self._started_at, 1),
            },
        }
        self.telemetry = frame

        broadcast_period = 1.0 / max(0.5, self.config.broadcast_hz)
        if now - self._last_broadcast >= broadcast_period:
            self._last_broadcast = now
            light = dict(frame)
            if light.get("map"):
                light["map"] = {**light["map"], "points": self.grid.point_cloud(max_points=1200)}
            self._publish(light)
        return frame

    # ------------------------------------------------------------------
    # public API used by the REST layer
    # ------------------------------------------------------------------
    def state(self) -> dict:
        snap = self.ekf.snapshot(time.time())
        return {
            "ekf": snap.as_dict(),
            "transform": self.ekf.transform(),
            "sensors": {d.name: d.info for d in self.drivers},
            "map": self.grid.stats().as_dict(),
            "diagnostics": self.diagnostics.as_dict(),
            "storage": self.store.stats(),
            "scenario": self.scenarios.snapshot(),
            "device": self.telemetry.get("device", {}),
            "config": self.config.as_dict(),
            "iterations": self._iterations,
            "running": self._running.is_set(),
        }

    def history(self, limit: int = 500, since: float | None = None) -> list[dict]:
        return self.store.history(limit=limit, since=since)

    def map_payload(self, max_points: int = 4000) -> dict:
        payload = self.grid.to_dict(max_points=max_points)
        payload["walls"] = self.walls
        payload["ground_truth"] = self.world.as_dict()
        return payload

    def mesh(self, height: float = 2.7) -> dict:
        segments = extract_walls(self.grid.occupied_cells()) if self.grid.updates else []
        x0, y0, x1, y1 = self.world.bounds()
        return build_mesh(segments, height=height, floor_bounds=(x0, y0, x1, y1))

    def start_scenario(self, params: ScenarioParams) -> dict:
        run_id = self.store.start_scenario(params.scenario, params.as_dict())
        run = self.scenarios.start(params, run_id=run_id)
        self._active_run_id = run_id
        self.store.save_event("scenario", {"action": "start", "run_id": run_id, "params": params.as_dict()})
        return run.snapshot()

    def stop_scenario(self) -> dict | None:
        metrics = self.scenarios.stop()
        if metrics and self._active_run_id:
            self.store.finish_scenario(self._active_run_id, metrics, status="stopped")
            self.store.save_event("scenario", {"action": "stop", "run_id": self._active_run_id, "metrics": metrics})
            self._active_run_id = None
        return metrics

    def save_map(self) -> dict:
        stats = self.grid.stats().as_dict()
        map_id = self.store.save_map(
            self.grid.snapshot_bytes(),
            self.grid.resolution,
            (float(self.grid.origin[0]), float(self.grid.origin[1])),
            self.grid.cells,
            stats,
        )
        return {"map_id": map_id, "stats": stats}

    def load_map(self, version: int | None = None) -> bool:
        record = self.store.load_map(version=version)
        if not record or record["cells"] != self.grid.cells:
            return False
        self.grid.load_bytes(record["grid"])
        return True

    def apply_config(self, patch: dict) -> list[str]:
        changed = self.config.apply(patch)
        if "sensors.mmwave_reduced" in changed:
            self.mmwave.configure_profile(self.config.sensors.mmwave_reduced)
        return changed
