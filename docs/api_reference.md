# AURA 6.0 — API reference

Base URL: `http://<agent>:8080` · Interactive docs: `/api/docs` · OpenAPI: `/api/openapi.json`

Through the visualiser, the same routes are reachable at `http://<visualizer>:3000/api/...`.

## Authentication

Optional. Set `AURA_API_TOKEN` and every `/api/*` route requires:

```
Authorization: Bearer <token>
```

`/health` stays open so load balancers can probe it. WebSocket clients pass
`?token=<token>`; a wrong token closes the socket with code 4401.

---

## Meta

### `GET /health`
```json
{ "status": "ok", "version": "1.0.0", "uptime": 13.1, "simulate": true,
  "iterations": 258, "clients": 1,
  "sensors": { "lidar": true, "mmwave": true, "uwb": true,
               "ble": true, "imu": true, "thermal": true } }
```
`status` is `degraded` when any driver has not produced a sample in 5 s.

### `GET /api/v1/agent/info`
Lists available scenarios, all registered routes and whether auth is on.

---

## State

### `GET /api/v1/agent/state`
Full snapshot: EKF state and covariance, per-driver health, map statistics,
fusion diagnostics, storage statistics, active scenario, effective config.

```json
{ "ekf": { "position": [9.92, 6.86, 1.37],
           "position_sigma": [0.025, 0.025, 0.11],
           "orientation_quaternion": [0, 0, 0.17, 0.98],
           "covariance_diagonal": [...15 values...],
           "converged": true },
  "map": { "cells_known": 22782, "cells_occupied": 1274, "coverage": 0.0989 },
  "diagnostics": { "sources": { "imu": 720, "uwb_range": 1440, "lidar_scanmatch": 700 } } }
```

### `GET /api/v1/agent/telemetry`
The most recent broadcast frame — the same object the WebSocket streams.

### `GET /api/v1/agent/history?limit=500&since=<unix>`
Persisted pose history, oldest first.

### `GET /api/v1/agent/events?source=<s>&limit=200`
Raw sensor/scenario events from the store.

---

## Map & export

| Route | Method | Purpose |
|---|---|---|
| `/api/v1/agent/map?max_points=4000` | GET | point cloud, extracted walls, ground truth |
| `/api/v1/agent/map/save` | POST | snapshot the grid as a new version |
| `/api/v1/agent/map/versions` | GET | list saved versions |
| `/api/v1/agent/map/load?version=N` | POST | restore a version |
| `/api/v1/agent/mesh?height=2.7` | GET | extruded triangle mesh |
| `/api/v1/agent/export/gltf?height=2.7` | GET | glTF 2.0 download (BIM-importable) |
| `/api/v1/agent/export/json?limit=2000` | GET | full state + map + history |
| `/api/v1/agent/export/cot?include_contacts=true` | GET | Cursor-on-Target XML for TAK. **409** unless `AURA_GEO_ANCHOR` is set — see `docs/tactical_integration_assessment.md` |
| `/api/v1/agent/export/mesh?limit=200` | GET | compact binary frame for one LoRa packet (8+22n bytes). Same 409 rule as `export/cot` |
| `/api/v1/agent/geo/anchor` | GET | current WGS84 anchor, or `configured:false` |
| `/api/v1/agent/geo/anchor` | POST | set the anchor from a GNSS fix; `sigma_m` feeds every exported `ce` |

---

## Scenarios

### `POST /api/v1/agent/scenario/start`
```json
{ "scenario": "evacuation", "people": 24, "smoke_density": 0.35,
  "panic": 0.3, "duration": 180, "speed": 1.0,
  "fire_source": [10.0, 4.0], "exits": [], "seed": 1234 }
```
`scenario` ∈ `evacuation | tactical | architecture | event | research`.
Returns the initial snapshot. Unknown scenario → 400; out-of-range → 422.

### `POST /api/v1/agent/scenario/stop`
Returns final metrics: `escaped`, `casualties`, `stranded`,
`evacuation_time_p50/p95/max`, `bottlenecks`. **409** if nothing is running or
the run was already stopped.

### `POST /api/v1/agent/scenario/pause` — `{ "paused": true }`
### `GET  /api/v1/agent/scenario` — active run + last 20 completed runs

---

## Radio Tomographic Imaging

### `POST /api/v1/agent/rti/configure`
```json
{ "nodes": [{ "id": "n0", "x": 6.2, "y": 3.0 }, ...],
  "min_x": 0, "min_y": 0, "max_x": 6, "max_y": 6,
  "resolution": 0.25, "alpha": 0.05, "use_l1": true }
```
→ `{ "links": 66, "voxels": 576, "lipschitz": 352.38 }`

Needs ≥ 3 nodes (400 otherwise) and ≤ 20 000 voxels (400 otherwise — the
weight matrix is `links × voxels` and grows quickly).

### `POST /api/v1/agent/rti/measure`
```json
{ "rssi": { "n0|n1": -50.0, "n0|n2": -51.2, ... }, "calibrate": false }
```
Call with `"calibrate": true` several times in an **empty room** first; the
attenuation image is the drop relative to that baseline. Measuring before
calibrating returns 409.

→ image, `targets` (weighted centroids), `iterations`, `converged`, `sparsity`.

**Accuracy:** ~0.18 m in the 12-node synthetic test, **1–2 m realistically** in
the field. See `docs/performance_targets.md`.

### `GET /api/v1/agent/rti`
Current configuration and last reconstruction.

---

## Passive radar

### `GET /api/v1/agent/radar/limits?bandwidth_hz=2400000&carrier_hz=626000000&integration_time=0.1`
```json
{ "range_resolution_m": 62.46, "velocity_resolution_ms": 2.3945,
  "doppler_resolution_hz": 10.0,
  "note": "range resolution is c/(2B); an RTL-SDR's 2.4 MHz gives ~62 m ..." }
```
These are physical limits. No processing improves them.

### `POST /api/v1/agent/radar/process`
```json
{ "sample_rate": 2400000, "samples": 32768, "carrier_hz": 626000000,
  "eca_taps": 16, "max_range_bins": 48, "num_batches": 64,
  "threshold_db": 10.0, "targets": [[20, 400.0, 0.03]],
  "direct_path_gain": 1000.0 }
```
With no SDR attached the IQ pair is synthesised from `targets`
(`[delay_samples, doppler_hz, amplitude]`), so the endpoint is exercisable
without hardware. Returns CFAR detections, the range-Doppler map and the
resolution limits that applied.

---

## Voxels

### `GET /api/v1/agent/voxels?threshold=1000&max_points=20000`
→ `{ stats: { chunks, occupied_voxels, compression_ratio }, labels, points }`
where each point is `[x, y, z, intensity, label]`.

### `POST /api/v1/agent/voxels/ingest?label=2`
Body: `[[x, y, z], ...]`. Labels: `0` empty · `1` structure · `2` person ·
`3` device · `4` hazard · `5` exit.

---

## Audit chain

### `GET /api/v1/agent/audit?limit=50&severity=security`
### `POST /api/v1/agent/audit`
```json
{ "actor": "operator", "action": "marker.place",
  "payload": { "x": 3.2, "y": 4.1 }, "severity": "notice" }
```
### `GET /api/v1/agent/audit/verify`
```json
{ "valid": true, "first_bad_index": null,
  "message": "chain of 42 entries is intact", "head": "36a8459a..." }
```
On tampering, `valid` is false and `first_bad_index` points at the altered
entry.

### Sensor health fields

Every entry under `sensors` in `/state` carries, in addition to the driver's
own `connected` / `age_seconds` / `healthy`:

| field | meaning |
|---|---|
| `health` | `ok`, `degraded`, `stuck` or `stale` |
| `health_reason` | human-readable evidence, e.g. `payload unchanged for 40 samples (4.0 s)` |

`healthy` is the **conjunction** of the driver's own view and the fault check,
so a sensor that is connected and delivering frames on time but repeating the
same payload reports `healthy: false`. Samples from a non-`ok` driver are not
fused; each skip increments `diagnostics.rejected_updates`. See
`docs/sensor_health.md`.

---

## UWB TDoA

### `POST /api/v1/agent/uwb/tdoa`

Hyperbolic multilateration of a **cooperating** UWB tag from arrival-time
differences. It does not locate unknown or non-transmitting devices — see
`docs/uwb_tdoa.md` §2.

```json
{
  "anchors": [{"id": "a0", "x": 0, "y": 0}, {"id": "a1", "x": 10, "y": 0},
              {"id": "a2", "x": 10, "y": 8}, {"id": "a3", "x": 0, "y": 8}],
  "tdoa_m": {"a1": 2.0, "a2": 0.44, "a3": -2.22},
  "sync_sigma_ns": 0.1
}
```

`sync_sigma_ns` is **required** — 1 ns of anchor clock offset is 30 cm of
range error, so there is no safe default. `tdoa_m` omits the reference anchor
(differences are measured against it); pass `reference` explicitly if more
than one anchor is missing.

```json
{
  "fix": {"x": 3.4991, "y": 5.5026, "sigma_m": 0.1206, "residual_m": 0.0031,
          "anchors_used": 4, "iterations": 5, "gdop": 0.817},
  "diagnostic_only_fix": null,
  "usable_sync": true,
  "sync_range_sigma_m": 0.03,
  "max_usable_sync_ns": 10.0,
  "warning": null,
  "note": null
}
```

- `sigma_m` combines the geometry (GDOP), the ranging noise **and** the clock
  sync term. It is never smaller than the sync floor.
- Above `max_usable_sync_ns` the fix is **withheld**: `fix` becomes `null` and
  the position moves to `diagnostic_only_fix`, so no client renders a marker
  from a sync figure that cannot support one.
- `fix: null` with `diagnostic_only_fix: null` means no solution at all — under
  3 differences, collinear anchors, GDOP > 20, or divergence.
- **400** if fewer than 3 anchors or an anchor coordinate is non-finite;
  **422** if `sync_sigma_ns` is missing.

---

## Configuration & mesh

| Route | Method | Purpose |
|---|---|---|
| `/api/v1/agent/config` | GET/POST | loop rate, broadcast rate, retention, sensor modes |
| `/api/v1/agent/tokens` | GET/POST | BLE token registry |
| `/api/v1/agent/tokens/{address}` | DELETE | remove a token (404 if absent) |
| `/api/v1/agent/projects` | GET | list surveys |
| `/api/v1/agent/ingest` | POST | secondary device uploads RSSI/position |
| `/api/v1/agent/devices` | GET | recently seen mesh devices |

---

## WebSocket — `/ws/agent/events`

On connect the server sends a `hello` frame containing the world geometry,
then telemetry at `broadcast_hz` (default 10 Hz).

### Telemetry frame
```json
{ "type": "telemetry", "timestamp": 1786578468.9, "iteration": 1440,
  "pose": { "x": 7.89, "y": 2.61, "yaw": 0.337 },
  "ekf": { ... }, "lidar": { "angles": [...], "distances": [...] },
  "map": { "points": [[x,y,z,i], ...], "walls": [...], "stats": {...} },
  "ble": { "rssi": {...}, "beacons": [...] },
  "uwb": { "ranges": {...}, "los": {...}, "vitals": {...} },
  "people": [...], "through_wall": [...], "vitals": {...},
  "scenario": {...} | null,
  "device": { "battery": 97.7, "temperature": 32.2, "loop_hz": 32.4 } }
```

### Commands (client → server)
```json
{ "command": "scenario.start", "payload": { "people": 30 } }
```
`ping` · `state` · `map` · `history` · `scenario.start` · `scenario.stop` ·
`scenario.pause` · `config` · `ingest`. Unknown commands and malformed JSON
both return an `error` frame rather than closing the socket.

---

## Error handling

| Code | Meaning |
|---|---|
| 400 | semantically invalid (unknown scenario, too few RTI nodes, oversized grid) |
| 401 | missing or wrong bearer token |
| 404 | unknown resource (e.g. deleting a token that is not registered) |
| 409 | wrong state (stop with no run; measure before calibrate) |
| 422 | schema/range violation (Pydantic) |
| 503 | (visualiser proxy only) the edge agent is unreachable |
