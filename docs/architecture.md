# AURA 6.0 — Architecture

## 1. Overview

AURA is a three-tier platform for capturing, fusing and visualising the
environment around a Honeywell CT45P-X0N handheld:

```
┌──────────────────────────────────────────────────────────────────────┐
│ CT45P-X0N  (Android 11, QCS4290, 4 GB)                               │
│                                                                       │
│  Kotlin layer                                                         │
│    sensors/   LiDAR · mmWave · BLE · IMU · UWB drivers               │
│    fusion/    SensorFusionService  (foreground service, 20 Hz)       │
│    storage/   LocalVectorStore     (SQLite + WAL)                     │
│    security/  CausalValidator      (SHA-256 audit chain)             │
│    llm/       LLMService           (llama.cpp, optional)             │
│    ui/        4 tabs; the map tab hosts the shared Babylon bundle    │
│                                                                       │
│  Native layer (aura_core.so, C++17 + NEON)                           │
│    ExtendedKalmanFilter   15-state, Joseph-form                       │
│    reconstructL1Fista     RTI compressed sensing                      │
│    ecaCancel / computeCaf passive radar                               │
│    encodeChunk            voxel RLE                                   │
└───────────────┬──────────────────────────────────────────────────────┘
                │ REST + WebSocket (8080)          MQTT (1883, optional)
┌───────────────▼──────────────────────────────────────────────────────┐
│ Edge agent  (Python 3.11, FastAPI, Docker)                           │
│    aura/ekf.py         reference EKF (the Kotlin/C++ port target)    │
│    aura/fusion.py      the control loop                              │
│    aura/mapping.py     occupancy grid, wall extraction, glTF         │
│    aura/rti.py         FISTA reference implementation                │
│    aura/passive_radar  CAF, ECA, CFAR                                │
│    aura/voxel.py       chunked sparse voxels + SVO                   │
│    aura/scenarios.py   agent-based evacuation model                  │
│    aura/audit.py       hash-chained audit log                        │
│    aura/storage.py     SQLite/WAL context store                      │
└───────────────┬──────────────────────────────────────────────────────┘
                │ HTTP + WS (3000)
┌───────────────▼──────────────────────────────────────────────────────┐
│ Web visualiser  (Node 18+, Express, Babylon.js 7)                    │
│    server.js           static host + REST proxy + WS bridge          │
│    src/SceneManager    thin-instanced scene graph                    │
│    src/DataFetcher     telemetry socket with backoff                 │
│    src/SimControls     sidebar, layers, export                       │
│    src/demo-simulator  synthetic scene when no agent is running      │
└──────────────────────────────────────────────────────────────────────┘
```

## 2. Why the same algorithm exists three times

`ekf.py`, `aura_core.cpp` and `NativeEkf.kt` implement the same filter. This
is deliberate, not duplication by accident:

* **Python** is the *reference*. It is where the maths is developed and where
  the Jacobians are checked against numerical differentiation
  (`test_rotation_jacobian_matches_numeric`). It is readable and slow.
* **C++** is the *production* path on the device. It is a line-by-line port
  and is validated against numbers the Python produced
  (`test_aura_core.cpp`: RTI 0.177 m vs Python's 0.180 m).
* **Kotlin** is a *thin facade*. It holds no algorithm — only a native handle,
  argument validation and lifecycle. Anything else would be a third place for
  the maths to drift.

The rule: **if the maths changes in Python, the C++ test must be updated in the
same commit.** The cross-check numbers in `test_aura_core.cpp` are the contract.

## 3. The fusion loop

One iteration at `loop_hz` (default 20 Hz), in `aura/fusion.py::tick`:

```
1. scenario.step(dt)              advance the agent-based simulation
2. imu.read()  -> ekf.predict()   strap-down propagation
   ├ StaticDetector -> ZUPT       only on a genuinely still window
   └ magnetometer  -> yaw update
3. uwb.read()
   ├ bootstrap the pose if the filter is not yet anchored
   ├ range update per anchor (σ inflated for NLOS, never dropped)
   ├ altitude prior (device carried at ~1.4 m)
   └ CIR sample -> MicroDopplerAnalyzer
4. lidar.read() -> ScanMatcher -> ekf.updateLidarPose(x, y, yaw)
   └ integrate the sweep into the occupancy grid
5. ble.read()  -> multilateration -> gated position update
6. mmwave.read() + thermal.read() -> PersonTracker
   └ occluded detections + vitals -> ThroughWallTracker
7. persist a transform record every `persist_every` iterations
8. publish a telemetry frame to WebSocket subscribers at `broadcast_hz`
```

### Ordering matters

UWB is read **before** LiDAR because the filter must be anchored in the map
frame before the scan matcher starts. If mapping begins while the EKF is still
at the origin, the matcher locks onto a self-consistent map that is offset from
the anchor coordinates — we measured a 3.6 m error before fixing this.

## 4. State estimation

15-state EKF:

```
x = [ px py pz | vx vy vz | roll pitch yaw | bgx bgy bgz | bax bay baz ]
      position   velocity    ZYX Euler       gyro bias     accel bias
```

* **Joseph-form covariance update** — `(I−KH)P(I−KH)ᵀ + KRKᵀ`. The short form
  `(I−KH)P` loses symmetry after a few thousand updates and eventually goes
  indefinite. `test_covariance_stays_symmetric_positive_definite` guards this.
* **Analytic Jacobians** — `∂(R·a)/∂euler` in closed form, verified against
  central differences to 1e-5.
* **Angle wrapping everywhere** — yaw innovations go through `wrap_pi`, so a
  measurement at −179° against a state at +179° is a 2° correction, not 358°.

### Measurement models

| Source | Observes | σ | Note |
|---|---|---|---|
| UWB range | ‖p − anchor‖ | 0.12 m LOS / 0.55 m NLOS | never discard NLOS |
| LiDAR scan match | x, y, yaw | 0.06 m / 0.10 rad | **not z** |
| BLE multilateration | x, y | ≥ 1.5 m, gated | outliers rejected at 3σ |
| Magnetometer | yaw | 0.35 rad | weak, indoors it is noisy |
| ZUPT | v = 0 | 0.02 m/s | windowed detector only |
| Altitude prior | z | 0.45 m | keeps the vertical channel observable |

## 5. Mapping

* **Occupancy grid** — log-odds, Bresenham ray carving, `+0.85` on a hit and
  `−0.28` along the ray. Clamped to ±[4, 5] so the map can still forget.
* **Wall extraction** — RANSAC-lite line fitting followed by a total-least-
  squares refit, throttled to every 4 s because it is the most expensive step.
* **Mesh + glTF** — walls extruded to boxes, packed into a self-contained glTF
  2.0 document with a base64 buffer (no sidecar `.bin` to lose).
* **Voxels** — 16³ chunks, RLE-compressed, floor-division chunk indexing so
  negative coordinates work. Measured 178–403× compression on sparse scenes.

## 6. Scenario engine

Hybrid flow-field + social-force model:

* **Flow field** — multi-source BFS from every exit over a traversability grid.
  Gives each agent a globally sensible direction, so nobody wedges in a dead
  end the way pure local steering does.
* **Local repulsion** — inverse-square-ish push between agents within 0.9 m.
  This is what produces realistic doorway congestion, which is the output
  planners actually care about.
* **Smoke** — diffusion + buoyant drift on a 0.5 m grid, blocked by walls.
  Slows agents and accumulates a toxicity dose; exceeding it marks a casualty.
* **Collision** — agents that would enter a wall slide along it instead of
  stopping dead. `test_agents_never_walk_through_walls` checks every agent on
  every tick.

## 7. Security model

* **Audit chain** — `h_n = SHA256(h_{n-1} ‖ canonical_json(entry))`. Canonical
  JSON with sorted keys is what makes a chain written on the CT45P verifiable
  on the agent. Optional HMAC key turns it into a MAC chain.
* **Timestamp inside the payload** — a validator that mixes `time.time()` into
  the digest at hash time can never reproduce it later. This is the single
  easiest way to build an unverifiable audit log.
* **Tamper localisation** — `verify()` returns the first bad index, so an
  investigator learns *where* the log was altered, not just *that* it was.
* **Network** — cleartext is allowed only for the mesh subnet in
  `network_security_config.xml`; everything else requires TLS.
* **Backup exclusion** — survey data and the audit chain are excluded from
  Android cloud backup and device transfer.

## 8. Data flow to the browser

The browser never contacts the agent directly. Two reasons, both practical:

1. In the field the agent is on a private mesh address (`10.8.0.1`) that an
   observer's phone cannot route to.
2. Behind any reverse proxy — including this project's preview environment —
   `localhost:8080` in browser JavaScript refers to the *browser's* machine.

So `server.js` proxies `/api/*` and bridges `/ws`. Client code uses only
same-origin relative URLs.

## 9. Testing

| Suite | Count | What it covers |
|---|---|---|
| `test_ekf.py` | 12 | kinematics, Jacobians, updates, numerical stability |
| `test_sensors.py` | 30 | protocol parsers, simulators, vital signs, FPR |
| `test_mapping_storage.py` | 24 | grid, RANSAC, glTF, SQLite, retention |
| `test_scenarios_fusion.py` | 25 | flow field, smoke, tracker, full pipeline |
| `test_aura6.py` | 43 | RTI, passive radar, voxels, audit chain |
| `test_api.py` | 31 | REST, auth, WebSocket, AURA 6.0 routes |
| `test_aura_core.cpp` | 618 assertions | the native port, cross-checked vs Python |

Run everything:

```bash
cd edge-agent && python -m pytest tests/ -q
cd android-app/app/src/main/cpp/tests && \
  g++ -std=c++17 -O2 -I.. test_aura_core.cpp ../aura_core.cpp -o /tmp/t && /tmp/t
```
