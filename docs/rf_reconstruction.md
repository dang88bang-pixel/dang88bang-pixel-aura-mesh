# Reconstructing the environment from radio, without a camera

> "es geht im kern der app darum aus drahtlosnetzwerkdaten und ähnliche daten
> zur exakten generierung der umgebung zu erstellen auch ohne direkten
> sichtkontakt oder kameraaufnahmen"

That is the right goal, and it is largely what AURA already does. This
document separates the parts of the accompanying proposal that are real from
the parts that do not survive contact with the physics, and records the one
capability gap the review found — which is now closed.

**The headline correction:** the proposal treats Wi-Fi CSI as the primary way
to reconstruct *the environment*. It is not. Wi-Fi CSI sensing — and AURA's own
RTI — measure **change**, not **structure**. They find *people* through walls.
They cannot image the wall. Those are different problems with different
physics, and conflating them is the single most consequential error in the
document.

## 1. The distinction that governs everything

Every radio sensing method falls into one of two families.

### Family A — "what moved?" (baseline-subtracting)

RTI, Wi-Fi CSI sensing, passive radar, micro-Doppler. All of them work by
removing a static reference and looking at the residual.

This is not an implementation detail we could optimise away. It is definitional,
and **AURA's own code already states it**:

```python
# aura/rti.py
def calibrate(self, rssi):
    """Accumulate an empty-room baseline (running mean)."""
    ...
def update(self, rssi):
    # attenuation is a *drop* relative to the empty-room baseline
    shadowing = np.maximum(self.baseline - y, 0.0)
```

A wall that is present during calibration **and** afterwards contributes
identically to both terms and cancels exactly. `tests/test_rf_mapping.py::
test_rti_is_blind_to_static_structure` proves this against our implementation
rather than asserting it in prose: feed an unchanged room in and the
reconstructed image is zero to within 1e-6.

The literature says the same thing:

- MIT's **Wi-Vi** explicitly *nulls* reflections off static objects, including
  the wall, before it can see anything behind it.
- **SiWiS** (MobiCom '24): *"Like a Doppler radar, it is suited for detecting
  moving objects, not static objects."*
- **Wi-Depth** (arXiv 2503.06458) reconstructs depth images "of moving objects
  **without any static components such as backgrounds**."

So the honest claim for this family is: *detect and locate people through
walls, without line of sight*. AURA does that today, via RTI and UWB
micro-Doppler. It is genuinely impressive. It is not environment
reconstruction.

### Family B — "what is there?" (time-of-flight)

To measure a stationary surface you need round-trip time, and range resolution
is `c / (2B)`. Bandwidth is therefore the whole game:

| Sensor | Bandwidth | Range resolution | Static geometry? |
| --- | --- | --- | --- |
| BLE advertising channel | 1 MHz | 150 m | no |
| RTL-SDR (passive radar) | 2.4 MHz | 62.5 m | no |
| Wi-Fi 20 MHz | 20 MHz | 7.5 m | no |
| Wi-Fi 80 MHz | 80 MHz | 1.88 m | marginal |
| **UWB DW3000** | 500 MHz | **30 cm** | yes, sparse |
| **IWR6843 mmWave FMCW** | ~4 GHz | **3.75 cm** | **yes** |

This is why the answer to "generate the environment exactly, without a camera"
is **mmWave FMCW radar**, not Wi-Fi. A 4 GHz sweep resolves structure at
centimetre scale and does so on stationary objects, because FMCW recovers range
from beat frequency rather than from motion.

AURA already had an `IWR6843` driver. It was only being used to find people.

## 2. What changed: mmWave now builds the map

Before this commit, `FusionPipeline` kept only the *moving* mmWave returns:

```python
moving = [t for t in targets if abs(t.velocity) > 0.18 and t.track_id >= 0]
```

Everything else — every stationary return, i.e. **all of the geometry** — was
discarded. The consequence was that the occupancy grid was fed exclusively by
`self.grid.integrate_scan(...)` on the LiDAR path. **No LiDAR meant no map.**
For a product whose premise is camera-free reconstruction, that made the core
promise depend on an optical sensor.

Static returns are now integrated into the same occupancy grid:

```python
structure = [
    t for t in targets
    if abs(t.velocity) <= MMWAVE_STATIC_VELOCITY      # 0.05 m/s
    and t.snr >= MMWAVE_STATIC_MIN_SNR                # 12 dB
    and MMWAVE_STATIC_MIN_RANGE <= t.range_m <= MMWAVE_STATIC_MAX_RANGE
]
```

Three deliberate choices:

- **The static gate (0.05 m/s) sits below the people gate (0.18 m/s), and the
  band between is left unclaimed.** A return is never both a wall and a person.
  A slow return is ambiguous, and painting a walking person into the map as a
  wall is much worse than a sparser map.
- **SNR floor of 12 dB.** Below that, multipath ghosts dominate and would be
  written in as phantom walls — the classic failure of naive radar mapping.
- **Range window 0.35–12 m.** Closer is antenna crosstalk; further is beyond
  the IWR6843's useful indoor structure range.

The telemetry payload now reports `structure` and `mapped_cells` alongside
`moving`, so an operator can see how much of the map came from radar.

**Proof it works:** `test_map_builds_from_mmwave_alone_without_lidar` disables
the LiDAR entirely (`pipe.lidar.read = lambda: None`) and asserts the grid still
fills. Mutation-tested — disabling the new integration makes exactly 2 tests
fail, so they are load-bearing rather than decorative.

## 3. Wi-Fi CSI on the CT45P: not available at all

Independently of the physics above, the proposal's Wi-Fi CSI pipeline cannot
run on this hardware.

**CSI is not exposed by standard Android.** The framework offers RSSI, not
per-subcarrier amplitude and phase. Extracting CSI requires patched firmware,
and the tooling is chipset-specific:

| Tool | Chipsets | CT45P (Qualcomm QCS4290)? |
| --- | --- | --- |
| Nexmon CSI | Broadcom/Cypress bcm4339, bcm43455c0, bcm4358, bcm4366c0 | **no** |
| Intel 5300 CSI Tool | Intel 5300 NIC | no |
| Atheros CSI Tool | Atheros AR9380/9580 | no |
| ESP32-S3 | Espressif | only as an *external* sensor |

Nexmon works by reverse-engineering **Broadcom** firmware. The CT45P is
Qualcomm. There is no port, and producing one would mean reverse-engineering
and reflashing the Wi-Fi firmware of a locked-down rugged enterprise device.

**Even RSSI is rate-limited.** Since Android 9, a foreground app gets **4
`startScan()` calls per 2 minutes**; background apps share one per 30 minutes.
The developer-options throttle toggle is a per-device manual setting, not
something an app can rely on in the field. Wi-Fi scanning is therefore unusable
as a real-time sensing channel here — roughly 0.033 Hz against the ~10 Hz the
fusion loop runs at.

`CSI2PointCloud`, `CSI2Depth` and `LatentCSI` are real research and the
repositories exist. But they are trained per-room, evaluated offline on
research NICs, and — per their own papers — reconstruct *moving* subjects, not
room geometry. LatentCSI in particular generates a *plausible* image via Stable
Diffusion; for a system whose output informs operational decisions, a
hallucinated wall is a safety problem, not a feature.

**Where CSI is genuinely usable:** an **ESP32-S3 over USB-OTG** as an external
CSI sensor. That sidesteps the chipset problem entirely, costs a few euros, and
would slot into the existing `SensorDriver` interface. It still only gives
Family-A (motion) sensing — but it is the honest route if CSI is wanted.

## 4. Corrected capability table

| Goal | Proposal's method | Works? | What actually delivers it |
| --- | --- | --- | --- |
| Locate people through walls | Wi-Fi CSI | **yes, in principle** | already shipped: RTI + UWB micro-Doppler |
| Vital signs without contact | CSI / mmWave | **yes** | already shipped: `VitalsEstimator` |
| **Map static geometry, no camera** | Wi-Fi CSI / CSI2PointCloud | **no** — baseline-subtracting | **mmWave FMCW — added in this commit** |
| Metric ranging to anchors | Wi-Fi RTT (802.11mc) | conditional | needs HAL flag + 802.11mc APs; UWB already covers this |
| CSI extraction on CT45P | Nexmon | **no** — Broadcom only | external ESP32-S3 over USB-OTG |
| High-rate Wi-Fi scanning | `getScanResults()` | **no** — 4 per 2 min | BLE scanning (unthrottled) |
| Photorealistic scene from radio | LatentCSI + SD3 | research only | not appropriate: generated ≠ measured |

## 5. Accuracy: what "exact" can mean here

The word *exakt* in the request deserves a direct answer, because the ceiling
is set by physics, not by software effort.

**RTI is severely underdetermined.** With `K` nodes there are `K(K-1)/2` links,
and each link yields exactly one number (the RSSI drop along its whole path):

| Nodes | Links = measurements | Grid unknowns | Ratio |
| --- | --- | --- | --- |
| 6 | 15 | 576 | 38× underdetermined |
| 12 | 66 | 576 | 8.7× underdetermined |
| 30 | 435 | 576 | 1.3× underdetermined |

AURA's configured grid is 24×24 = 576 voxels against 66 links. This is only
solvable because the L1/FISTA prior assumes the image is **sparse** — i.e. "a
few people are somewhere in here". Feed it a dense scene like a floor plan and
the assumption breaks. Measured RTI localisation lands at **1–2 m**, as already
recorded in [`performance_targets.md`](performance_targets.md).

**mmWave gives real geometry** at 3.75 cm range resolution, but cross-range
resolution is limited by antenna aperture (`≈ λR/D`), so a wall is built up
over multiple viewpoints as the operator moves, exactly like the LiDAR path.

The honest summary: **centimetre-accurate structure from mmWave; metre-accurate
people from RTI/UWB; nothing at all from Wi-Fi CSI on this device.**

## 6. Recommendation

1. **Done here — mmWave feeds the map.** This is the direct answer to the core
   requirement and it removes AURA's hidden dependency on an optical sensor.
2. **Next, if more coverage is wanted: more UWB anchors, not Wi-Fi.** At 500 MHz
   they give 30 cm ranging and already have a driver and a protocol
   ([`uwb_anchor_protocol.md`](uwb_anchor_protocol.md)).
3. **Optional: ESP32-S3 CSI sensor over USB-OTG** for motion sensing in rooms
   with no anchors. Cheap, and honest about what it measures.
4. **Do not build a Wi-Fi CSI pipeline on the CT45P.** No chipset support, no
   Nexmon port, and scan throttling would cap it at ~0.033 Hz regardless.

*Verified 2026-08-13 against AURA's own RTI implementation, the Nexmon CSI
supported-chipset list, Android Wi-Fi scan-throttling documentation, the
Android `WifiRttManager` requirements, Wi-Vi (SIGCOMM '13), SiWiS (MobiCom '24)
and Wi-Depth (arXiv 2503.06458).*
