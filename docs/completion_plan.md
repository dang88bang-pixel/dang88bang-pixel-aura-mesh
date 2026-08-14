# Completion plan — what is still missing

An audit of what is actually wired up, as opposed to what exists as a file.
Compiled 2026-08-13 by cross-referencing the class inventory, the layout ids,
the manifest and the spec against the code that uses them.

The headline: **the edge agent and the web visualiser are complete and
verified. The Android tier has all its parts written but most of them are not
connected to anything.** Five subsystems are never instantiated, and 23 of 27
layout ids are never read by code — the three secondary fragments inflate XML
and stop there.

## How each item was established

| evidence | command |
|---|---|
| never-instantiated classes | `grep -rn "ClassName(" --include=*.kt` minus the declaration |
| dead layout ids | `comm -23` of ids in `res/layout/*.xml` vs `R.id.*` in Kotlin |
| endpoint coverage | each spec route grepped in `api.py`, then curled live |
| button coverage | every `id="btn-*"` in `index.html` grepped in `src/*.js` |

---

## A. Android — subsystems written but never reached

| # | item | evidence | consequence | effort |
|---|---|---|---|---|
| ~~**A1**~~ | ~~`LiveViewFragment` shows no data~~ | — | **DONE** `f4741fc`. Attitude, pose, sweep, RSSI bars, vitals, device health. Added `VitalsEstimator` (27 host tests). | M |
| ~~**A2**~~ | ~~`ScenarioFragment` inert~~ | — | **DONE**. Spinner + 3 sliders + start/stop/progress/metrics, verified end-to-end against the running agent. | M |
| ~~**A3**~~ | ~~`SettingsFragment` inert~~ | — | **DONE**. Agent URL, mmWave power, on-device audit verification, storage and native diagnostics. | M |
| ~~**A4**~~ | ~~`AgentApiClient` never constructed~~ | — | **DONE**. One shared instance on `AuraApplication`, URL persisted and shared with the WebView. | S |
| ~~**A5**~~ | ~~`NativeRti` never constructed~~ | — | **DONE**. `configureRti()`/`onRtiSample()` on the service, guarded by a voxel ceiling. Pipeline verified against the Python reference: attenuating the crossing links localises a target at (3.0, 3.0), the exact intersection. | S |
| ~~**A6**~~ | ~~`NativePassiveRadar` never constructed~~ | — | **DONE** `2026-08-14`. `configureRadar()` constructs it on the service; `onIqSamples()` runs process → CFAR → detect. Until an SDR transport exists the feed is `RadarSimulator` (same physics as `passive_radar.py`), so the path is exercised end to end like the other sensors; the sim's delay/Doppler conventions are host-tested (24 checks). | M |
| ~~**A9**~~ | ~~`NativeVoxelCodec` never wired; `voxelIngest` wrote a placeholder event~~ | — | **DONE** `2026-08-14`. `VoxelChunkWriter` accumulates sweep occupancy, RLE-encodes touched chunks via the native codec and persists them into `spatial_chunks` at 1 Hz. Grid maths host-tested (39 checks). | M |
| ~~**A7**~~ | ~~`LLMService` never constructed~~ | — | **DONE**. Constructed on `AuraApplication`; Settings tab loads a side-loaded GGUF (never bundled) and asks. Missing model / stub build is reported as such, and the tab still shows the live survey snapshot rather than a dead form. `VectorStore` extracted and host-tested (NaN similarity cannot rank first). | M |
| ~~**A8**~~ | ~~`fab_save_map` does nothing~~ | — | **DONE**. Saves a map snapshot to the audit chain. `toolbar` is decorative and intentionally unbound. | XS |

## B. Correctness risks (from `docs/android_build.md`)

| # | item | status |
|---|---|---|
| **B1** | USB-serial wiring | **done** (commit `0c2906c`) |
| **B2** | UWB on API 30 | **done** (reflective path, `0c2906c`) |
| ~~**B3**~~ | ~~`VpnService` single-instance~~ | **DONE**. `prepare()` is checked first; if another tunnel holds the interface the service refuses rather than tearing down the link the operator depends on. |
| ~~**B4**~~ | ~~Foreground-service types on Android 14~~ | **DONE**. The type mask is assembled from permissions actually granted, so a survey with BLE but no location still runs instead of dying with a `SecurityException`. |

## C. Verification gaps

| # | item | note |
|---|---|---|
| **C1** | No Android build possible | `dl.google.com` + Maven Central blocked; all mirrors tried and unreachable. Static gates only. |
| **C2** | Kotlin coverage is partial | 9 suites are host-testable (audit, UWB geometry, quality tiers, geo anchor, vitals, vector store, baud, voxel grid, radar sim) = 286 checks. Anything touching `Context` still needs an emulator. |
| **C3** | RPLIDAR S2 baud was the last open hardware claim | **done** — Slamtec FAQ [D7]: S2/S3 are 1 000 000, not 256000. Remaining unverified items are project-specific (Aura anchor protocol vs stock DWM3000 firmware). |

## D. Confirmed complete (no action)

- **Edge agent** — all 6 spec routes live, 41 routes total, 300 tests.
- **Web visualiser** — all 10 buttons wired; `export/gltf` and `export/json`
  both return 200 against the running agent.
- **Native core** — 634 checks; JNI 28/28 both directions.
- **Avatars, smoke, halos, point cloud, timeline** — present in `SceneManager`.
- **WebXR** — absent, and deliberately: it needs HTTPS plus a headset, neither
  of which applies to a handheld CT45P. Recorded here so it is not mistaken
  for an oversight.

---

## Order of work

Dependency-driven, most user-visible first:

1. ~~**A4** `AgentApiClient` wiring~~ — **done**
2. ~~**A1** Live view~~ — **done**
3. ~~**A2** Scenario control~~ — **done**
4. ~~**A3** Settings + audit verification~~ — **done**
5. ~~**A8** map snapshot button~~ — **done**
6. ~~**A5** RTI entry point~~ — **done**
7. ~~**B4** foreground-service permission ordering~~ — **done**
8. ~~**B3** VPN mutual-exclusion guard~~ — **done**
9. ~~**A6** passive radar~~ — **done** (simulated feed; real SDR still needs hardware)
10. ~~**A7** LLM assistant UI~~ — **done**
11. ~~**A9** voxel chunk writer~~ — **done**

Each step: implement → extend the static gates where the failure would
otherwise be silent → run all gates → commit → push.

## Gates added while doing this work

Each exists because the failure it catches is silent — it compiles, renders,
and misbehaves only in front of a user.

| gate | catches |
|---|---|
| `check-string-formats.py` | `getString` argument/specifier mismatch → `IllegalFormatConversionException` at render time |
| `check-dead-ui.py` | interactive views that render but are wired to nothing (this project had 23) |
| `check-usb-ids.py` | hex/decimal slips in the USB filter → sensor silently never detected |
| `check-android-deps.py` | imports with no resolvable coordinate or repository |
