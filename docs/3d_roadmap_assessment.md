# 3D roadmap — feasibility assessment

A proposal was put forward listing "weitere Möglichkeiten der 3D-Generierung,
-Darstellung & -Interaktion" for AURA, in five areas, each with an effort
rating. This document checks those ratings against the hardware AURA actually
targets before any of them enters the backlog.

The short version: **the proposal's effort ratings are close to inverted for
this device.** The two items it rates cheapest (ARCore Depth API, 3D Gaussian
Splatting on the handheld) are the two that the CT45P cannot run at all, and
the one thing that is genuinely free — WebGPU with a WebGL2 fallback — it
describes as an engine upgrade that is not in fact needed.

This is the same failure mode as the DWM3000 protocol documented in
[`uwb_anchor_protocol.md`](uwb_anchor_protocol.md): a plausible capability
list, assembled without checking it against the device in the holster.

## The device, restated

Every judgement below follows from these five facts. They are not estimates.

| Property | Value | Consequence |
| --- | --- | --- |
| SoC | Qualcomm QCS4290, 11 nm | Snapdragon 6xx-class, not a flagship |
| CPU | 8× Kryo 260 @ 2.0 GHz | ~Snapdragon 660 |
| GPU | **Adreno 610** | roughly half an Adreno 619; ~1/20th of an Adreno 750 |
| Memory bandwidth | ~13.9 GB/s LPDDR4X | the binding constraint for splatting |
| OS | **Android 11 (API 30)** | below the WebGPU floor |
| GMS | SKUs exist **Non-GMS** (e.g. `CT45P-X0N-38D200G`) | no Play Services for AR |
| Rear camera | single 13 MP, autofocus | **no ToF / depth sensor** |

And one architectural fact: the Map tab renders through an **Android WebView**
(`file:///android_asset/visualizer/index.html`), not Chrome.

## §1 — NeRF / 3D Gaussian Splatting, text-to-3D

**Verdict: not on the handheld. Viable offline, on the command-post PC.**

The proposal cites mobile 3DGS framerates without noting which silicon
produced them. Tracked down:

- **Mobile-GS** reports 116 FPS at 1600×1063 — on a **Snapdragon 8 Gen 3**
  (Adreno 750), and only after spherical-harmonic distillation, vector
  quantisation and pruning to reach a 4.8 MB model.
- **StreamingGS** measures *vanilla* 3DGS at **2–9 FPS on mobile**, and
  4.9–8.5 FPS on a Jetson Orin NX — hardware well above the CT45P. Its
  finding is that **DRAM bandwidth**, not shader throughput, is the wall.
- **MEGS²** reports that WebGL 3DGS with spherical harmonics "fails to run on
  mobile platforms" without 6–8× compression.

The Adreno 610 is roughly 1/20th of an Adreno 750. Scaling the *best published
flagship number* down by that factor lands near **6 FPS**, before applying the
bandwidth penalty that StreamingGS identifies as dominant — and the CT45P has
~13.9 GB/s to work with. There is no configuration in which this is a live
view on this device.

Text-to-3D (WorldGen, TRELLIS, Hunyuan 3D, Wonder 3D) is a different matter: it
is an **offline asset-authoring** step, run on a workstation, output as glTF.
That is compatible with AURA today — `/api/v1/agent/export/gltf` already
exists. It generates *plausible* geometry, though, not *measured* geometry, so
it must never be mixed into the fused map that operational decisions are made
from. Briefing and training scenery only, clearly labelled.

## §2 — WebGPU rendering, Potree point clouds

**Verdict on WebGPU: worth doing, already done, and free — but not for the
CT45P.** See "What changed in the code" below.

Two independent blockers stop WebGPU reaching the handheld:

1. Chrome enabled WebGPU by default on **Android 12+**. The CT45P is
   Android 11.
2. The Chromium intent-to-ship explicitly states WebGPU was **not** enabled
   for **Android WebView**. AURA's Map tab is a WebView.

Either alone is decisive; both apply. The Map tab will run WebGL2 for the life
of this hardware.

The proposal's premise that this requires **Babylon.js 8 or 9 is simply
wrong**. Verified against the installed tree:

```
$ node -p "require('@babylonjs/core/package.json').version"
7.54.3
$ ls node_modules/@babylonjs/core/Engines/ | grep -E 'webgpuEngine|engineFactory'
engineFactory.js
webgpuEngine.js
```

Babylon 7.54.3 already ships `WebGPUEngine` and `EngineFactory.CreateAsync`,
and `CreateAsync` already falls back to WebGL2 by itself. No upgrade, no
migration, no risk. Babylon 8's WGSL-native shaders would shrink the WebGPU
bundle, but that is an optimisation for the desktop client, not a prerequisite.

**Verdict on Potree: no. Wrong order of magnitude.** Measured against the
running agent:

```
$ curl -s 'localhost:8097/api/v1/agent/map?max_points=40000'
points returned : 2412        over 173.15 m²   →  13.9 points/m²
```

Extrapolating that measured density:

| Scene | Area | Points | Raw size |
| --- | --- | --- | --- |
| single room | 40 m² | ~560 | negligible |
| building floor | 1 500 m² | ~21 000 | 0.3 MB |
| large warehouse | 20 000 m² | ~279 000 | 4.5 MB |

Potree exists to stream 10⁸–10⁹-point laser scans from a **precomputed
octree on disk**. AURA's cloud is **live**, pushed over a WebSocket and
re-rendered as it changes. Even the warehouse case fits in a single Babylon
buffer. Adopting Potree would mean bolting an offline octree build onto a
real-time feed — a format mismatch that buys nothing.

## §3 — Eye-hand VR interaction, WebXR hand-tracking, voice control

**Verdict: hand-tracking is blocked by the same wall as §1; voice is viable.**

Per Google's own WebXR requirements: *"AR experiences on Android can only run
on an ARCore-supported device with Google Play Services for AR installed and
enabled."* WebXR `immersive-ar` on Android **is** ARCore. And in phone AR mode
there is no hand tracking at all — input is a screen tap reported as a
transient pointer. Hand tracking is a headset capability.

So "WebXR hand-tracking on the CT45P" is doubly unavailable: no ARCore (§4),
and no hand tracking in phone AR even where ARCore runs. On a **tethered
headset driven by the command-post PC** it is real, and that is the only
honest place to put it on a roadmap.

Voice-driven manipulation is the one genuinely cheap item in the whole
proposal: it needs no GPU, no ARCore and no new sensor, and AURA already
carries an on-device LLM (`LLMService`, Qwen2.5-1.5B Q4_K_M). Gloved,
eyes-up operation is also the interaction mode that actually suits this
device. If anything from §3 is scheduled, it should be this.

## §4 — Mobile 3D reconstruction via ARCore Depth API, Kiri Engine, Dot3D

**Verdict: not possible on this hardware. The proposal rates this "gering"
(low effort); the correct rating is "cannot be done".**

Three independent reasons, any one sufficient:

1. **No Honeywell device appears on any ARCore supported-device list.**
   Checked the community-maintained device CSV and Google's published list;
   the vendor is absent entirely. ARCore is an explicit allowlist — unlisted
   means unsupported, not "probably fine".
2. **Non-GMS SKUs cannot run it under any circumstances.**
   `CT45P-X0N-38D200G` ships Android 11 **Non-GMS**. ARCore requires Google
   Play Services for AR. On a Non-GMS unit that package cannot be installed.
3. **No depth camera.** The CT45P has a single 13 MP rear camera and no ToF
   sensor. ARCore's Depth API can infer depth monocularly on supported
   devices, but that is a per-device-calibrated feature gated by the same
   allowlist as (1).

Kiri Engine and Dot3D inherit these constraints — Dot3D's value is with
depth-sensor hardware the CT45P does not have.

If photogrammetric capture is genuinely wanted, the honest route is:
**capture stills on the CT45P, reconstruct off-device** on the command-post PC
or edge-agent, and import the result as glTF. That works today and needs no
new device capability.

## §5 — Corrected summary table

Effort ratings restated against the measured hardware. Changes from the
proposal are marked.

| # | Capability | Proposal | **Actual** | Basis |
| --- | --- | --- | --- | --- |
| 1 | 3DGS live on CT45P | mittel | **not possible** ⬇ | Adreno 610; vanilla 3DGS is 2–9 FPS on far stronger silicon |
| 1 | 3DGS / NeRF offline, viewed at command post | mittel | **feasible** | desktop GPU; export as glTF |
| 1 | Text-to-3D scenery | mittel | **feasible, with a caveat** | generated ≠ measured; briefing/training only |
| 2 | WebGPU on command-post PC | mittel | **done, free** ⬆ | Babylon 7.54.3 already ships it; see below |
| 2 | WebGPU on CT45P Map tab | mittel | **not possible** ⬇ | Android 11 < 12, and WebView is excluded |
| 2 | Babylon 8/9 upgrade | required | **not required** ⬇ | 7.54.3 has `WebGPUEngine` + `EngineFactory` |
| 2 | Potree point clouds | mittel | **not warranted** ⬇ | 2 412 pts measured; Potree targets 10⁸⁺, and offline |
| 3 | WebXR hand-tracking on CT45P | mittel | **not possible** ⬇ | needs ARCore; phone AR has no hand tracking |
| 3 | Hand-tracking on tethered headset | mittel | **feasible** | command-post PC drives the headset |
| 3 | Voice-driven 3D manipulation | mittel | **feasible, recommended** ⬆ | no GPU/ARCore needed; `LLMService` exists; suits gloves |
| 4 | ARCore Depth API | **gering** | **not possible** ⬇⬇ | no Honeywell device on the list; Non-GMS SKUs; no ToF |
| 4 | Kiri Engine / Dot3D | gering | **not possible** ⬇ | inherit the ARCore/depth constraints |
| 4 | Capture on device, reconstruct off-device | — | **feasible** ⬆ | added; the workable version of §4 |

## What changed in the code

Only the one item that verification actually supported.

`web-visualizer/src/main.js` now selects its engine via
`EngineFactory.CreateAsync`, so the command-post PC gets WebGPU where the
browser has it and everything else silently gets WebGL2. The active backend is
shown in the FPS readout so a field report can state it rather than guess.

Two deliberate details:

- **No top-level `await`.** Top-level await makes the whole module async at
  parse time and needs Chromium 89+. Android 11 shipped WebView 85, and a
  Non-GMS CT45P has no Play Store to update it through — the module would fail
  to parse and the Map tab would go blank with nothing in the log. Engine
  creation is wrapped in a `boot()` function instead.
- **The probe is skipped entirely when `navigator.gpu` is absent**, which is
  the CT45P case, so the handheld does not pay for a promise round-trip on the
  slowest device we target.

Reviewing that file also surfaced a **pre-existing bug**, now fixed. The
`visibilitychange` handler re-registered the render loop with a closure that
called only `scene.render()`, omitting the `pendingFrame` drain in the original
loop:

```js
// before — telemetry stops being applied after the tab is hidden and reshown
else engine.runRenderLoop(() => { scene.render(); });
```

The viewport kept rendering but stopped ingesting telemetry, silently freezing
the scene on the last frame before the tab was hidden. On the handheld this is
easy to hit — the Map tab is backgrounded every time the user switches tabs.
Both registrations now share one named `renderFrame` function.

## Recommendation

Of the thirteen items, three are worth scheduling:

1. **Voice-driven interaction** (§3) — cheapest real win, suits gloved use, and
   the on-device LLM is already there.
2. **Offline reconstruction on the command-post PC** (§1/§4) — captures the
   intent behind §4 without needing ARCore.
3. **WebGPU on the desktop client** (§2) — already shipped in this commit at
   zero cost.

The rest should not be planned against this hardware. They are not "hard"; they
are blocked by an allowlist, an OS floor and a GPU class, none of which a
software decision can move. If mobile 3D capture is a genuine requirement, that
is a **device** conversation — a GMS, ARCore-certified, Android 12+ handset —
not a backlog item.

*Verified 2026-08-13 against the CT45P/CT45 XP configuration guide, Qualcomm
QCS4290 specifications, Chrome/Chromium WebGPU shipping notes, Google's WebXR
requirements, the ARCore supported-device list, and the running agent.*
