# Deep research: what is actually still broken

Findings from measuring the system rather than reading it, 2026-08-13. Every
number here was produced by a command in this repository against the running
stack; the commands are given so each can be re-run.

Ordered by consequence, not by effort.

---

## 1. ~~The position estimate can be catastrophically wrong and still look fine~~ — **FIXED** (`420fc9e`)

### What was measured

The EKF was driven with realistic MEMS noise and then all aiding was removed,
simulating what happens when the operator walks out of BLE/UWB coverage into a
stairwell or basement.

| aiding lost for | planar drift | reported σ | honest? |
|---|---|---|---|
| 10 s | 1.48 m | 6.59 m | yes |
| 30 s | 23.9 m | 93.2 m | yes |
| 60 s | 161.5 m | 618 m | yes |
| 300 s | 18 038 m | 79 358 m | yes |

With a simulated gait (2 Hz stride, stance + swing phases) it is worse:

| condition | 60 s drift | reported σ | ZUPTs fired |
|---|---|---|---|
| standing still, ZUPT enabled | **0.01 m** | 0.05 m | 1200 / 1200 |
| standing still, ZUPT disabled | 103 m | 597 m | 0 |
| **walking** | **4777 m** | 589 m | **0 / 1200** |

Note the last row: while walking, drift (4777 m) **exceeds** the reported
sigma (589 m). The filter is no longer conservative — it is overconfident by
roughly 8×.

### Why ZUPT never fires while walking

`StaticDetector` requires a 12-sample window (0.6 s at 20 Hz) of low variance.
A 2 Hz stride has a stance phase of ~0.175 s ≈ 3.5 samples, so **the window
always spans swing phase**. Measured: a stance-only window gives
`gyro_peak = 0.020` (limit 0.06, passes); a real gait window gives
`gyro_peak = 0.626`, exceeding the limit by 10×.

This is **not a tuning bug**. It is the correct behaviour for a handheld
device, and the literature is explicit: *"the smartphone cannot detect the
real zero-velocity status, which makes it impossible to use the traditional
ZUPT algorithm"* ([MDPI Sensors 18(10) 3349](https://www.mdpi.com/1424-8220/18/10/3349)).
Classical ZUPT is a **foot-mounted** technique. Loosening the thresholds so it
fires during a stride would clamp velocity mid-step and make the estimate lag
reality — worse than not firing at all.

### Why this matters more than the numbers suggest

With all sensors present the filter is tight — measured against the live agent
over 40 samples: **max planar σ = 0.028 m, converged 40/40**. The danger is
not normal operation, it is the transition into sensor-denied space, which is
exactly when someone is deepest inside a building.

And the UI cannot express it. The only quality signal is a **binary**
`converged = max(pos_sigma) < 0.75`, rendered as `FIX` / `UNSICHER`
(`SimControls.js:115`) and `OK` / `…` (`MainActivity.kt:310`). **Between 0.75 m
and 4.7 km the operator sees exactly the same thing.**

For context, the applicable standards:

- **NIST PSCR**: better than **3 m 3D at 95 %**, without beacons
  ([NIST](https://www.nist.gov/ctl/pscr/first-responder-location-and-mapping-services))
- **FCC E911 z-axis**: **±3 m for 80 %** of calls (47 CFR § 9.10)

A 3 m 95 % requirement corresponds to σ ≤ 1.23 m in 2D. So the existing 0.75 m
threshold is a reasonable *good* boundary — the problem is that there is
nothing above it.

### Fix

1. Replace the boolean with graded tiers derived from those standards:

   | tier | σ | meaning |
   |---|---|---|
   | GOOD | ≤ 0.75 m | room-level |
   | DEGRADED | 0.75–3 m | still inside the NIST envelope at ~1σ |
   | POOR | 3–10 m | wrong room, right building |
   | LOST | > 10 m | **must not be drawn as a position** |

2. Track time-since-last-aiding and surface it. Drift is a function of that,
   not of anything the operator can see today.
3. Above the LOST threshold, draw an uncertainty disc rather than a point. A
   sphere at a definite location is a claim the filter is not making.

*Reproduce:* the drift table and gait comparison scripts in this section.

---

## 2. ~~The DWM3000 wire protocol is invented~~ — **FIXED**

`$INIT` / `$RANGE` / `$STOP` and the reply
`ANCHOR-A=3.214,ANCHOR-B=7.882;CIR=0.42,1.87` appear in both
`UwbManager.kt:220` and `edge-agent/aura/sensors/uwb.py:134`, described as
"the DWM3000 shell format".

Searching Qorvo's own documentation and forum: the DWM3001CDK ships a
**CLI/UCI application over USB CDC**, whose command set is defined by that
firmware and not by any published wire standard. There is no `$RANGE`
command in it. The confirmed facts are that the board exposes a virtual COM
port on the user USB connector (J20) and that a `UART` command redirects the
console to the RPi header.

So the parser is well-tested against a format **we made up**. It will not talk
to a stock DWM3001CDK.

This is not necessarily wrong — a custom firmware on the anchor is a legitimate
design — but it must be labelled as *our* protocol, not the vendor's, and the
firmware that implements it has to be written and shipped alongside.

### Fix

Rename the constants and comments to state that this is the **Aura anchor
protocol**, document it as a specification the anchor firmware must implement,
and record the alternative (speak UCI to stock firmware) with its cost.

---

## 3. ~~`armeabi-v7a` is built but cannot be reached~~ — **FIXED**

`abiFilters = ["arm64-v8a", "armeabi-v7a"]`, yet `minSdk = 30` and the CT45P is
arm64. Every 32-bit device that could load the v7a slice is excluded by the SDK
floor. The result is a second native build of the whole core, roughly doubling
native build time and APK size, for a slice nothing will ever load.

Keeping it is defensible only if a 32-bit accessory device is genuinely
planned. Otherwise it is cost with no benefit.

---

## 4. ~~`pipeline._iterations` and `store._conn`~~ — **FIXED**

`api.py:231` and `api.py:295` reach into private members across a module
boundary. Both are one-line accessor additions. Known debt, harmless today,
but it is exactly the kind of coupling that breaks silently during a refactor.

---

## Verified as *not* problems

Worth recording, because each was a plausible failure that turned out fine.

| checked | result |
|---|---|
| Covariance numerical stability | 36 000 steps (30 min): **0** non-positive-definite samples, symmetry error exactly 0, worst condition number 1.35e6 |
| EKF throughput | 9 486 steps/s — 470× the 20 Hz loop rate |
| WebSocket backpressure | a client that never reads does not starve others: the normal client still received **120 frames**, agent alive afterwards |
| Native handle lifecycle | `onDestroy` closes `rti`, `ekf` and `store`; `NativeEkf`/`NativeRti` null their handles |
| Unbounded growth | `cirHistory` is capped at 1200; `LocalVectorStore` has retention; the LLM `VectorStore` is bounded by survey size |
| Blocking calls on the fusion thread | `Thread.sleep` appears only in sensor reader threads, not the UI or fusion loop |

---

## Status

All four are fixed. What each turned into:

| § | fix |
|---|---|
| 1 | graded `quality` tiers (`good`/`degraded`/`poor`/`lost`) plus `seconds_since_aiding`, in Python, Kotlin and the visualiser, with a generated cross-platform fixture so the three cannot disagree |
| 2 | renamed to the **Aura anchor protocol** and specified in `docs/uwb_anchor_protocol.md`, stating plainly that stock firmware does not speak it |
| 3 | `abiFilters` reduced to `arm64-v8a` |
| 4 | `FusionPipeline.iterations` and `LocalVectorStore.connection` accessors |

## Still open, deliberately

| item | why |
|---|---|
| **A6** passive radar unreachable | needs an RTL-SDR on the USB port; wiring it blind adds an untestable path |
| **A7** LLM assistant UI | needs a GGUF model on-device |
| **C1** no Android compile here | no SDK, no route to `dl.google.com`. `ci/github-actions-apk.yml` is the fix, and it needs one manual activation step |
| **C2** Kotlin coverage is partial | anything touching `Context` needs an emulator; only pure logic is host-testable |
| DWM3000 hardware verification | the protocol is now specified, but no board has ever been connected |
