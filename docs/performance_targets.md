# Performance targets — measured, not aspirational

This document exists because the original AURA 6.0 specification contained
several targets that **physics or the CT45P hardware will not deliver**.
Shipping those numbers into an acceptance test would guarantee failure, so
each one is restated here with the achievable figure, the reason for the gap,
and what it would actually take to close it.

Everything in the "measured" column comes from the test suites in this
repository and can be reproduced with `pytest edge-agent/tests/` and the
native test binary.

---

## 1. Summary table

| Capability | Spec target | Realistic | Measured here | Verdict |
|---|---|---|---|---|
| RTI person localisation | < 0.5 m | 1–2 m (portable), 0.3–0.6 m (dense array) | **0.18 m** (12-node ring, 6×6 m, synthetic) | ⚠️ achievable only in the dense/ideal case |
| Passive radar range resolution | < 10 m | 62 m @ 2.4 MHz, 19 m @ 8 MHz | **62.46 m** @ 2.4 MHz | ❌ not achievable with an RTL-SDR |
| Offline LLM throughput | > 10 t/s (Phi-3) | 3–6 t/s (Phi-3), 10–16 t/s (1.5 B) | not benchmarkable in this sandbox | ⚠️ model swap required |
| EKF localisation | not specified | 0.1–0.5 m with UWB anchors | **0.15 m** mean, 0.19 m p95 | ✅ |
| UWB vital signs | not specified | ±2 breaths/min at ≤ 5 m, LOS-ish | **16.8 vs 16.8 bpm**, 0 % FPR | ✅ |
| Voxel compression | not specified | 10–100× on sparse scenes | **178–403×** | ✅ |
| Audit chain verification | 100 % | 100 % | **100 %**, tamper index localised | ✅ |
| Kotlin/Python hash parity | implied | byte-identical | **44/44 Kotlin checks**, digests pinned to Python | ✅ |
| Fusion loop rate | not specified | 10–30 Hz on-device | **32 Hz** (sandbox), 20 Hz configured | ✅ |
| WireGuard throughput | > 30 Mbit/s | 30–80 Mbit/s on QCS4290 | not benchmarkable | ⚠️ plausible |
| Voxel render rate | > 10 FPS | 30–60 FPS with thin instances | not benchmarkable (no GPU) | ⚠️ plausible |

---

## 2. Radio Tomographic Imaging: why "< 0.5 m" is conditional

RTI resolution is governed by **link density**, not by the solver. With `K`
nodes you get `K(K−1)/2` links; the reconstruction is an inverse problem whose
conditioning improves roughly with the number of links crossing each voxel.

| Nodes | Links | Typical accuracy | Notes |
|---|---|---|---|
| 4 | 6 | 2–4 m | barely more than presence detection |
| 8 | 28 | 1.5–2.5 m | usable for room-level occupancy |
| 12 | 66 | 0.8–1.5 m | **0.18 m in our noise-free-ish synthetic test** |
| 20 | 190 | 0.4–0.8 m | needs deliberate placement |
| 30+ | 435+ | 0.2–0.5 m | the configuration behind published sub-0.5 m claims |

Our measured 0.18 m (`test_fista_recovers_a_single_target`) uses a **12-node
ring around a 6×6 m room with σ = 0.01 measurement noise**. That is an
optimistic model: real RSSI has multipath fading of several dB, bodies are not
point targets, and node positions are known only approximately.

**Honest field expectation for a handheld/portable deployment: 1–2 m.**
Sub-0.5 m requires pre-installed infrastructure, which contradicts the
"grab-and-go CT45P" concept.

What the implementation does get right, and what measurably matters:

* **L1/FISTA over Tikhonov** — in `test_fista_is_sparser_than_tikhonov` the L1
  solution concentrates into **1 voxel** where Tikhonov smears across **192**.
* **Power-iteration Lipschitz constant** — 352 versus the naive `‖W‖²_F` bound
  of 1420. A 4× overestimate quarters the FISTA step size and makes the solver
  appear not to converge.

---

## 3. Passive radar: the resolution limit is not negotiable

Range resolution is `c / (2B)`. This is a property of the waveform bandwidth
and no amount of processing changes it:

| Illuminator / SDR | Usable bandwidth | Range resolution |
|---|---|---|
| RTL-SDR (RTL2832U) | 2.4 MHz | **62.5 m** |
| HackRF One | 20 MHz | 7.5 m |
| Full DVB-T channel, wideband SDR | 8 MHz | 18.7 m |
| DAB block | 1.5 MHz | 100 m |
| FM broadcast | 0.2 MHz | 750 m |

The spec's "< 10 m" needs ≥ 15 MHz of *coherently sampled* bandwidth — an
HackRF/USRP class device, not the RTL-SDR named in the same document.
`GET /api/v1/agent/radar/limits` returns this calculation live so an operator
cannot be misled by the UI.

**Doppler resolution has a matching hard limit: `1/T`.** This bit us during
development — a 16 384-sample dwell at 2.4 MSps is 6.8 ms, so Doppler cells are
~146 Hz wide and a 70 Hz target *cannot* be resolved from zero Doppler. The
direct CAF will happily evaluate at a 70 Hz grid point, but that is
interpolation inside one cell, not resolving power. See
`test_doppler_resolution_requires_a_long_enough_dwell`.

What does work well: **ECA clutter cancellation removes 75–93 dB** of
direct-path leakage in our tests (`test_eca_removes_the_direct_path`), which is
the difference between a usable range-Doppler map and a solid wall of noise.

---

## 4. Offline LLM on the CT45P-X0N

**Corrected 2026-08-13.** This section previously assumed 4 GB of RAM. That was
wrong, and the error came from conflating two models in the same family:

| model | RAM |
|---|---|
| CT45 (`CT45-L0N`, `CT45-L1N`) | 4 GB DDR4x |
| **CT45 XP (`CT45P-X0N`, `CT45P-L1N`)** | **6 GB DDR4x** |

The `P` in `CT45P-X0N` denotes the XP variant. Honeywell's configuration guide
lists `CT45P-X0N-38D100G` as "CT45XP, WLAN, **6GB**/64GB … USB 3.0 Type C OTG".
The target device therefore has 6 GB, not 4 GB.

Measured community figures for llama.cpp on this class of SoC (QCS4290):

| Model | Quant | File | Peak RAM | Tokens/s |
|---|---|---|---|---|
| Phi-3-mini 3.8B | Q4_K_M | 2.4 GB | ~2.8 GB | **3–6** |
| Phi-3-mini 3.8B | Q4_0 | 2.2 GB | ~2.6 GB | 4–7 |
| Qwen2.5 1.5B-Instruct | Q4_K_M | 1.0 GB | ~1.3 GB | **10–16** |
| Gemma 2 2B | Q4_K_M | 1.6 GB | ~1.9 GB | 7–11 |

**What the correction changes.** The original argument against Phi-3 had two
legs, and one of them has now fallen over:

- *Memory pressure* — "2.8 GB on a 4 GB device means the OS starts killing
  things". On 6 GB this is much weaker. Phi-3-mini is genuinely viable
  alongside the sensor pipeline, and calling it unusable would be wrong.
- *Throughput* — the ">10 t/s with Phi-3" figure in the source material comes
  from flagship 8-core SoCs with roughly twice the memory bandwidth. Decode
  speed on a quantised model is bandwidth-bound, not capacity-bound, so **this
  leg is unaffected by the RAM correction.** 3–6 t/s is roughly reading speed:
  usable for a short answer, painful for anything longer.

**Decision (unchanged, for a narrower reason):** `LLMService.DEFAULT_MODEL`
stays Qwen2.5-1.5B-Instruct Q4_K_M, now purely on latency rather than on RAM.
`LLMService.PHI3_MODEL` is a supported choice on this device rather than a
concession to hypothetical larger hardware. Both remain opt-in downloads — a
2.4 GB asset has no business inside the APK.

---

## 5. What we measured in this repository

### Localisation (EKF + UWB + LiDAR scan matching)
```
test_pipeline_localises_against_ground_truth
  mean error 0.15 m, p95 0.19 m over a 300-tick walked survey
test_pipeline_keeps_altitude_bounded
  z stays in 0.5–2.5 m (was drifting to 2.98 m before the fix in §6)
```

### UWB vital signs
```
test_micro_doppler_extracts_respiration_and_heartbeat
  0.28 Hz injected -> 16.79 bpm recovered; 1.15 Hz -> 68.8 bpm
test_micro_doppler_false_positive_rate_on_noise
  0/60 false detections on pure noise (was 33 % before the fix in §6)
```

### Kotlin audit chain (44 checks, `tools/run-kotlin-tests.sh`)
```
canonical JSON, chain integrity, tamper/delete/reorder detection,
HMAC forgery rejection, export/restore round trip, severity filtering
cross-platform: Kotlin digest == Python digest (pinned fixtures)
```
Runs on a plain JVM with kotlinc - no Android SDK, no Gradle, no Robolectric -
so the logic that decides whether a survey is admissible is verified even where
the Android toolchain is unavailable.

### Native core (618 assertions, `test_aura_core.cpp`)
```
RTI peak error        0.177 m   (Python reference: 0.180 m)
ECA cancellation      75.6 dB
UWB trilateration     0.0061 m
stationary EKF drift  0.0000 m over 20 s
voxel chunk           4096 voxels -> 34 bytes
```

---

## 6. Bugs the tests caught

These are recorded because each one silently degraded a headline number, and
each was found by a test rather than by inspection.

| Bug | Symptom | Fix |
|---|---|---|
| ZUPT fired during walking | position lagged **1.45 m** behind truth | windowed `StaticDetector` (variance over 12 samples) instead of a single-sample check |
| LiDAR update fed its own `z` back | altitude drifted to **2.98 m** | `update_lidar_pose` observes (x, y, yaw) only — a 2D scan match cannot see altitude |
| First IMU sample differentiated from zero | **850 m/s²** spike at startup | clamp world acceleration to ±6 m/s² |
| Vital-sign gate at 6 dB | **33 % false-positive rate** — "person breathing" in an empty building | raise to 18 dB (noise p95 = 8.7 dB, real breathing = 33 dB) + require 3 consecutive windows |
| EKF never anchored to the map frame | scan matcher locked onto a self-consistent but **3.6 m offset** map | bootstrap the pose from UWB/BLE trilateration before mapping starts |
| NLOS UWB anchors discarded | 1–2 usable anchors left the solution sliding along an unobservable direction | fuse all anchors, inflate σ to 0.55 m for NLOS instead of dropping |
| `express.json()` before the proxy | every proxied POST hung until timeout | mount the body parser *after* the `/api` proxy |
| Double scenario stop | returned 200 and duplicated history | `reaped` flag → 409 on the second call |
| **Kotlin collapsed `1000.0` to `1000`** | canonical JSON differed from Python, so **every** audit entry written on a CT45P failed verification on the agent (every timestamp is a float) | render doubles exactly as Python's `json.dumps`; pinned by cross-platform fixtures |
| **`(1.001 * 1000.0).toLong()` floored to 1000** | IEEE-754 gives `1000.9999999999999`; every restored entry not on a whole second failed to verify | `Math.round` instead of truncation |
| `viewpager2` imported but never declared | guaranteed `assembleDebug` failure — it is not transitive via material/appcompat | added the dependency |
| `androidx.activity.result` imported but never declared | same, for the permission launcher | added `activity-ktx` |
| C++ exported `NativePassiveRadar_*`, no such Kotlin class | the entire passive-radar path was unreachable from the app | wrote `NativePassiveRadar.kt`; now machine-checked |
| `LLMService` loaded a library that did not exist | four `external fun`s with no symbol → `UnsatisfiedLinkError` indistinguishable from a crash | `llama_bridge.cpp`, stub by default, real backend behind `-DAURA_WITH_LLAMA=ON` |
| `GatekeeperVpnService` declared in the manifest, class absent | manifest-merger/runtime failure | implemented the service |
| No launcher icon, empty Gradle wrapper dir | build could not produce an APK | vector adaptive icon + wrapper properties and script |

---

## 7. Legal and regulatory constraints (DE / EU)

Not performance, but equally capable of stopping a deployment.

**Spectrum.** UWB in the EU is limited to 6–8.5 GHz with an EIRP of
−41.3 dBm/MHz (ETSI EN 302 065). Through-wall sensing at useful range tempts
operators to exceed this; doing so requires a BNetzA allocation. Passive radar
is *receive-only* and therefore unlicensed, but recording third-party
transmissions can engage §148 TKG.

**Data protection.** Respiration and heart rate are health data under GDPR
Art. 9 as soon as they are linkable to an identified person. In a building
where occupancy is known, "person behind wall 3, 17 breaths/min" is very likely
personal data. Required: a DPIA (Art. 35), a documented legal basis, and
retention limits — the agent's `enforce_retention` exists partly for this.

**Employee monitoring.** In Germany, deploying this in a workplace triggers
BetrVG §87(1)(6) co-determination. The works council must agree *before*
installation, not after.

**Emergency-services exemption.** BOS use during an actual incident has a much
wider legal basis (life-saving) than surveying or training use. The audit chain
(`CausalValidator`) exists so that the distinction is provable after the fact.

*This is engineering context, not legal advice. Get a lawyer before deploying.*
