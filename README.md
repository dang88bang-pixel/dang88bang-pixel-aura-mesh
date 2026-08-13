# AURA 6.0

**Multisensor fusion, 3D reconstruction and scenario analysis for the Honeywell CT45P-X0N.**

A three-tier platform that maps a building while you walk through it, detects
people behind walls from their breathing, and simulates evacuations on the map
it just built.

```
CT45P-X0N (Kotlin + C++/NEON)  ──REST/WS──▶  Edge agent (Python)  ──HTTP/WS──▶  Babylon.js viewer
   sensors · EKF · audit chain                 fusion · RTI · radar              3D · scenarios
```

---

## Status

| Component | State | Verification |
|---|---|---|
| Edge agent (Python) | working, runnable | **162 tests pass** |
| Native core (C++17/NEON) | compiles, cross-checked vs Python | **618 assertions pass** |
| Web visualiser (Babylon.js) | working, runnable | live against the agent at 10 Hz |
| Kotlin audit chain | compiles and runs on a host JVM | **44 checks pass**, digests pinned to Python |
| JNI layer | compiled against a stub `jni.h` | **28/28 symbols matched** both ways |
| Android app (full APK) | complete source, **never assembled** | no Android SDK reachable here |

Nuance on the last two rows: the Kotlin classes with no Android dependency —
above all `CausalValidator`, which decides whether a survey is admissible — are
compiled with `kotlinc` and executed in CI via `tools/run-kotlin-tests.sh`.
Everything touching `Context`, `SensorManager`, `SQLiteOpenHelper` or JNI still
needs a real `gradlew assembleDebug`, which this environment cannot run. See
[`docs/android_build.md`](docs/android_build.md).

---

## Quick start

```bash
# 1. edge agent (simulated sensors, no hardware needed)
cd edge-agent
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python agent.py                       # http://localhost:8080

# 2. visualiser
cd ../web-visualizer
npm install
npm start                             # http://localhost:3000
```

Open <http://localhost:3000>. With no agent running the UI falls back to a
built-in simulated survey, so it is never a black screen.

Docker:

```bash
cd edge-agent && docker compose up -d
```

---

## What actually works, with numbers

Every figure below comes from a test in this repository.

| Capability | Measured |
|---|---|
| Localisation (EKF + UWB + scan matching) | **0.15 m** mean, 0.19 m p95 |
| RTI person localisation (12 nodes, synthetic) | **0.18 m** |
| L1 vs Tikhonov sparsity | 1 voxel vs 192 |
| UWB respiration extraction | 16.8 bpm injected → **16.79 bpm** recovered |
| Vital-sign false positives on noise | **0 / 60** |
| Passive radar clutter cancellation | **75–93 dB** |
| Voxel compression (sparse scene) | **178–403×** |
| Audit tamper detection | exact index localised |
| Kotlin ↔ Python hash parity | byte-identical (pinned fixtures) |
| Fusion loop | 32 Hz measured (20 Hz configured) |

**And what does not:** passive radar range resolution is **62 m** with an
RTL-SDR, not the < 10 m in the original spec (`c/2B` is not negotiable), and
Phi-3-mini runs at **3–6 t/s** on the QCS4290, not > 10. Both are analysed
in [`docs/performance_targets.md`](docs/performance_targets.md), which also
lists the eight bugs the test suite caught.

---

## Repository layout

```
android-app/                     Kotlin + C++ for the CT45P
  app/src/main/cpp/              native core (no Eigen/FFTW dependency)
    aura_core.{h,cpp}            EKF · FISTA · CAF/ECA · voxel RLE
    aura_jni.cpp                 JNI bridge (handle-based, zero-copy)
    tests/test_aura_core.cpp     618 assertions, runs on any host
  app/src/main/java/com/aura/agent/
    fusion/    NativeEkf · SensorFusionService
    sensors/   LiDAR · mmWave · BLE · IMU + StaticDetector
    rti/       NativeRti
    security/  CausalValidator (audit chain)
    storage/   LocalVectorStore · NativeVoxelCodec
    network/   AgentApiClient (backoff + jitter)
    llm/       LLMService (llama.cpp + RAG)
    ui/        MainActivity · custom views

edge-agent/                      Python reference implementation
  aura/ekf.py                    15-state EKF, analytic Jacobians
  aura/fusion.py                 the control loop
  aura/rti.py                    FISTA compressed sensing
  aura/passive_radar.py          CAF · ECA · CFAR
  aura/voxel.py                  chunked sparse voxels + SVO
  aura/scenarios.py              flow-field + social-force evacuation
  aura/audit.py                  SHA-256 hash chain
  tests/                         162 tests

web-visualizer/                  Babylon.js 7
  server.js                      static host + REST proxy + WS bridge
  src/SceneManager.js            thin-instanced scene graph
  src/demo-simulator.js          synthetic scene when no agent runs

docs/
  architecture.md                design and rationale
  api_reference.md               38 routes, request/response shapes
  performance_targets.md         measured vs specified, and why
  android_build.md               how to actually assemble the APK
  source_claims.md               every hardware constant, and how sure we are
  3d_roadmap_assessment.md       proposed 3D features vs what the CT45P can run
  user_manual.md                 field guide (German)

tools/
  run-kotlin-tests.sh            host-JVM Kotlin suite (no Android SDK)
  setup-kotlin-toolchain.sh      fetch a JRE + kotlinc when none is installed
  check-android-deps.py          imports vs declared/fetchable dependencies
  ingest-reference-docs.py       extract the PDF/TXT reference set to text
  check-jni-symbols.py           external fun <-> Java_* symbol cross-check
  bundle-visualizer.sh           build the Babylon bundle into app assets
  generate_audit_fixtures.py     regenerate the cross-platform digests
```

---

## Running the tests

```bash
# Python: 162 tests
cd edge-agent && python -m pytest tests/ -q

# Native: 618 assertions, no Android toolchain required
cd android-app/app/src/main/cpp/tests
g++ -std=c++17 -O2 -I.. test_aura_core.cpp ../aura_core.cpp -o /tmp/aura_test
/tmp/aura_test

# Kotlin: 44 checks, needs only a JRE + kotlinc (no Android SDK)
tools/run-kotlin-tests.sh

# JNI: every `external fun` must have a matching native symbol
tools/check-jni-symbols.py --source-only
```

The native suite cross-checks the C++ port against values produced by the
Python reference — RTI peak error 0.177 m (C++) vs 0.180 m (Python). If the
maths changes in one, the other's test fails.

---

## Design decisions worth knowing

**The same EKF exists three times** (Python, C++, Kotlin facade) on purpose.
Python is the readable reference where Jacobians are checked numerically; C++
is the device path; Kotlin holds no algorithm at all, only a native handle.

**The scan matcher never observes altitude.** A 2D LiDAR match sees `(x, y,
yaw)`. Feeding the filter's own `z` back as a pseudo-measurement shrinks the
vertical covariance without adding information — that bug drifted altitude to
2.98 m before it was caught.

**NLOS UWB anchors are fused, not discarded.** With only 1–2 line-of-sight
anchors the range-only geometry is underconstrained and the estimate slides
along the unobservable direction. Inflating σ to 0.55 m beats dropping them.

**The browser never talks to the agent directly.** In the field the agent is on
a private mesh address; behind any proxy, `localhost:8080` in browser JS means
the *browser's* machine. Everything goes through the visualiser's proxy over
relative URLs.

**Doubles are rendered exactly as Python's `json.dumps` does.** Collapsing
`1000.0` to `1000` is the tidier-looking choice and it silently breaks the audit
chain across platforms — every timestamp is a float, so every entry written on
the handheld would fail verification on the agent. The cross-platform test pins
Kotlin's digests against real Python output.

**The presence gate is 18 dB, not 6.** At 6 dB the vital-sign detector had a
33 % false-positive rate on pure noise — it reported people breathing in empty
buildings. Noise peaks at ~10 dB, real breathing at 33 dB.

---

## Hardware

The CT45P-X0N itself has **no** LiDAR, mmWave, UWB or SDR. All of those attach
over USB-C host mode:

| Sensor | Device | Interface |
|---|---|---|
| LiDAR | RPLIDAR A1/A2/S2 | USB-serial (CP210x / FTDI) |
| mmWave | TI IWR6843 | USB-serial (XDS110, CLI + DATA) |
| UWB | Qorvo DWM3000 | USB-CDC or the Android UWB API (12+) |
| SDR | RTL-SDR / HackRF | USB (libusb) |
| Thermal | MLX90640 / FLIR Lepton | USB or SPI |
| IMU, BLE | built in | Android SensorManager / BluetoothLeScanner |

Drivers fall back to physics-based simulators when hardware is absent, which is
why the whole stack runs end-to-end on a laptop and in CI.

---

## Legal

Through-wall sensing and passive radar carry real constraints in DE/EU: UWB
EIRP limits (ETSI EN 302 065), GDPR Art. 9 for vital-sign data, BetrVG §87 for
workplace deployment. Summarised in
[`docs/performance_targets.md#7`](docs/performance_targets.md). Not legal
advice.

---

## Licence

Apache-2.0.
