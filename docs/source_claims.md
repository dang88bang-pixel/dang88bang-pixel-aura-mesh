# Hardware claims and where they come from

Every number in this project that constrains the hardware interface is listed
here, together with how confident we are in it and how to re-check it. The
purpose is narrow: to make it obvious which values are **verified against a
source document**, which are **reasoned from physics**, and which are
**assumptions that will break a real device if wrong**.

A claim nobody can re-check is indistinguishable from one that was invented.

> **Status of the reference documents.** The project's ~30 supporting documents
> (Honeywell CT45P specifications, BLE/UWB analyses, through-wall sensing
> studies, SDR notes) are *not* in this repository — they are large and mostly
> third-party. Run `tools/ingest-reference-docs.py --src <dir>` to extract them
> to `docs/reference/` (gitignored) and re-check the claims marked
> **NEEDS SOURCE** below.

## Legend

| mark | meaning |
|---|---|
| **VERIFIED** | confirmed against a primary source, cited inline |
| **PHYSICS** | derived from first principles; the derivation is in the code or in `performance_targets.md` |
| **NEEDS SOURCE** | taken from the project brief or from general knowledge, not yet confirmed against a document |
| **ASSUMPTION** | a deliberate engineering choice; wrong values degrade behaviour but are safe |

---

## Platform

| claim | value | status | how to re-check |
|---|---|---|---|
| CT45P-X0N Android version | Android 11, **API 30** | **NEEDS SOURCE** | Honeywell CT45P spec sheet. Drives `minSdk 30` and the whole UWB fallback design — if the device is actually API 31+, `androidx.core.uwb` becomes usable and `UwbManager`'s reflective path is dead weight. |
| CT45P has no UWB radio | no `android.hardware.uwb` | **NEEDS SOURCE** | Same spec sheet. If false, the DWM3000 serial path is still correct but no longer the *only* option. |
| RAM available to the app | ~4 GB device | **NEEDS SOURCE** | Determines the LLM choice (Qwen2.5-1.5B Q4_K_M at 1.0 GB rather than Phi-3-mini at 2.4 GB). |
| USB-C supports host mode (OTG) | yes | **NEEDS SOURCE** | Every external sensor depends on this. Without it the LiDAR/mmWave/UWB paths cannot work at all. |

## USB device identifiers

Used by `res/xml/usb_device_filter.xml` and the `UsbSerialTransport.forX()`
factories. A wrong VID means the device is simply never detected.

| device | VID / PID | status |
|---|---|---|
| RPLIDAR A1/A2 (CP2102) | `0x10C4` / `0xEA60` | **NEEDS SOURCE** — Silicon Labs CP210x, widely documented |
| RPLIDAR S2 (FTDI) | `0x0403` | **NEEDS SOURCE** |
| TI IWR6843 | `0x0451`, port 0 = CLI @115200, port 1 = DATA @921600 | **NEEDS SOURCE** — TI mmWave SDK docs |
| Qorvo DWM3000 | `0x0403` (FTDI) | **NEEDS SOURCE** |
| RTL-SDR | `0x0BDA` | **NEEDS SOURCE** |
| RPLIDAR S2 baud | 256000 | **NEEDS SOURCE** |

## Wire formats

| claim | value | status |
|---|---|---|
| DWM3000 shell commands | `$INIT`, `$RANGE`, `$STOP` (CRLF) | **NEEDS SOURCE** |
| DWM3000 range reply | `ANCHOR-A=3.214,ANCHOR-B=7.882;CIR=0.42,1.87` | **NEEDS SOURCE** — mirrored in both the Python driver and `UwbGeometry.parseLine`, so a change touches two places |
| NLOS marker | trailing `*` on a range | **NEEDS SOURCE** |
| RPLIDAR parser resyncs one byte at a time | — | **PHYSICS** — required by the sync-byte framing; verified by test |

## Performance figures

These replace the original brief's targets, which the physics does not
support. See `docs/performance_targets.md` for the full derivations.

| claim | value | status |
|---|---|---|
| Passive radar range resolution | ≈ 62 m @ 2.4 MHz | **PHYSICS** — `c / (2·B)`; measured 62.46 m |
| Doppler resolution floor | `1 / T` | **PHYSICS** — cannot be beaten by any estimator |
| Handheld RTI accuracy | ≈ 1–2 m (not the brief's <0.5 m) | **PHYSICS** + measured (FISTA α=0.05 → 0.18 m in a 12-node lab rig; degrades badly handheld) |
| Micro-Doppler presence threshold | 18 dB SNR + 3 persistence hits | **MEASURED** — 6 dB gives 33 % false positives; noise floor max 9.9 dB vs chest wall 33–41 dB → 0 % FP / 100 % TP |
| BLE RSSI smoothing | EMA α = 0.35 | **MEASURED** — raw RSSI is ±8 dB |
| EKF planar error (600-tick baseline) | mean 0.157 m / p95 0.185 / max 0.194 | **MEASURED** — regression baseline |
| UWB trilateration conditioning limit | cond ≤ 1000 | **MEASURED** — well-shaped rigs sit at 1–3; a 60 m × 1 mm sliver reaches 2.3e10 and turns 1 cm of noise into 833 m of error |

## Software coordinates

| claim | value | status |
|---|---|---|
| `usb-serial-for-android` 3.11.0 | published on **JitPack**, not Maven Central | **VERIFIED** — github.com/mik3y/usb-serial-for-android release 3.11.0, published 2026-07-18. Enforced by `tools/check-android-deps.py` |
| `androidx.core.uwb` requires API 31 | deliberately **not** declared | **VERIFIED** — a direct reference is a `NoClassDefFoundError` at verification time on API 30 |

---

## What to do when the documents are available

```bash
tools/ingest-reference-docs.py --src ~/uploads \
    --grep "CT45P" --grep "API 30" --grep "Android 11" \
    --grep "UWB" --grep "DWM3000" --grep "IWR6843" --grep "RPLIDAR"
```

Then, for each **NEEDS SOURCE** row above, either cite the document and
promote it to **VERIFIED**, or correct the value. The rows most worth checking
first are the ones that change the design rather than a constant:

1. **API level / UWB radio** — decides whether the reflective UWB fallback is
   necessary at all.
2. **USB host mode** — decides whether external sensors are viable.
3. **USB VIDs** — a wrong value silently prevents detection, and it is the
   failure mode hardest to diagnose in the field.
