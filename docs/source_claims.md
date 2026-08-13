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
>
> **2026-08-13:** the platform rows were verified directly against Honeywell's
> public datasheet and configuration guide instead, which found a **wrong
> assumption** — see the RAM row.

## Primary sources used

- **[D1]** Honeywell, *CT45 XP / CT45 Datasheet*, `prod-edam.honeywell.com`
  → `sps-ppr-ct45-ct45xp-mobile-computer-data-sheet-en-ltr.pdf`
- **[D2]** Honeywell, *CT45 / CT45 XP Configuration Guide*, Rev E
  (per-SKU memory, radio and I/O breakdown)
- **[D3]** Honeywell, *CT45 / CT45 XP User Guide* (model overview tables)

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
| Ships with Android 11 (**API 30**) | yes | **VERIFIED** [D1] | "Each Android version planned from Android 11 to Android 14". Ships on 11, so `minSdk 30` is right. But see the note below — the device is *upgradeable*, which the original design ignored. |
| Upgradeable through Android 13/14/15 | yes, "pending feasibility" | **VERIFIED** [D1] | Consequence: a fielded unit may well be API 33+. `UwbManager`'s reflective platform path is therefore **not** dead code, and `targetSdk 34` is correct. |
| CT45P has no UWB radio | correct — not listed | **VERIFIED** [D1] | The radio list is WWAN / WLAN / Bluetooth 5.1 + BLE / NFC / GPS. No UWB, on any SKU. The DWM3000-over-serial path is the only option. |
| Second BLE beacon ("Device Finder") | **CT45 XP only** | **VERIFIED** [D1] | Works with the main battery drained. Not currently exploited by the app — see "Opportunities" below. |
| RAM available to the app | **6 GB** DDR4x | **CORRECTED** [D1][D2] | ~~4 GB~~. `CT45P-X0N-38D100G` = "CT45XP, WLAN, **6GB**/64GB". The `P` marks the XP variant; only the plain CT45 has 4 GB. This weakened one of the two arguments for the default LLM — see `performance_targets.md#4`. |
| USB-C supports host mode (OTG) | yes, **USB 3.0 Type C OTG** | **VERIFIED** [D1][D2] | "USB OTG supported via I/O ports"; every SKU line in [D2] ends "USB 3.0 Type C OTG". The external-sensor architecture is sound. |
| Processor | Qualcomm QCS4290/QCM4290 octa-core 2.0 GHz | **VERIFIED** [D1] | Basis for the llama.cpp throughput figures. |
| Bluetooth | **5.1** + BLE | **VERIFIED** [D1] | Note: **not** 5.0 as the brief's title suggested, and BT 5.1 direction-finding (AoA/AoD) is an *optional* feature — do not assume it is present. |
| Sensors (IMU) | accelerometer, gyroscope, magnetometer, eCompass "model dependent" | **VERIFIED** [D1] | `ImuManager` must degrade gracefully if the magnetometer is absent on a given SKU. |
| Storage expansion | microSD up to 512 GB | **VERIFIED** [D1] | Sensible location for GGUF models and long scan sessions. |
| Battery | Li-Ion 3.85 V, 4020 mAh; warm swap on XP | **VERIFIED** [D1] | The 7000 mAh figure seen on some reseller pages is an extended pack, not standard. |
| Operating temperature | −20 °C to +50 °C | **VERIFIED** [D1] | |

## USB device identifiers

Used by `res/xml/usb_device_filter.xml` and the `UsbSerialTransport.forX()`
factories. A wrong id means the device is simply never detected. Enforced by
`tools/check-usb-ids.py`, which also verifies that each hex value in a comment
matches the decimal Android actually parses.

- **[D4]** USB-IF registry via `usb-ids.gowdy.us`, read 2026-08-13
- **[D5]** TI E2E: IWR6843ISK Rev C/D carry a CP2105, earlier boards an XDS110
- **[D6]** MathWorks *Radar Toolbox* setup guide: the IWR6843ISK config port is
  "Silicon Labs Dual CP2105 … Enhanced COM Port **or** XDS110 Class
  Application/User UART"; the data port is the Standard COM Port **or** the
  XDS110 Auxiliary Data Port
- **[D7]** Slamtec, *RPLIDAR FAQ*, wiki.slamtec.com/display/SD/RPLIDAR+FAQ,
  last modified 2026-02-04, read 2026-08-13. Baud-rate table for USB adapter
  boards and the explicit note "The baud rate for S2 is 1M."

| device | VID / PID | status |
|---|---|---|
| Silicon Labs CP210x UART Bridge (RPLIDAR A1/A2) | `0x10C4` / `0xEA60` | **VERIFIED** [D4] |
| Silicon Labs CP2105 Dual UART Bridge | `0x10C4` / `0xEA70` | **VERIFIED** [D4] — **added**, see below |
| FTDI FT232 Serial (UART) IC (RPLIDAR S2) | `0x0403` / `0x6001` | **VERIFIED** [D4] |
| FTDI FT2232C/D/H Dual UART/FIFO (DWM3000 carrier) | `0x0403` / `0x6010` | **VERIFIED** [D4] |
| TI XDS110 debug probe (IWR6843) | `0x0451` / `0xBEF3` | **VERIFIED** [D4] — exposes an Application/User UART **and** an Auxiliary Data Port, which is what the two-port design relies on |
| Realtek RTL2838 DVB-T (RTL-SDR) | `0x0BDA` / `0x2838` | **VERIFIED** [D4] |
| Realtek RTL2832U DVB-T (RTL-SDR) | `0x0BDA` / `0x2832` | **VERIFIED** [D4] — **added** |
| RPLIDAR S2 baud | **1 000 000** (was wrongly 256000) | **VERIFIED** [D7] | Slamtec *RPLIDAR FAQ*, wiki.slamtec.com/display/SD/RPLIDAR+FAQ, read 2026-08-13: "Black housing without DIP switch **1000000:S2,S3**" and "The baud rate for S2 is 1M." 256000 is the S1 / A3 / A2M7 rate. `LidarBaud.forVendor` now picks 1 Mbaud for an FTDI bridge and 115200 for a CP2102. |
| IWR6843 CLI @115200 / DATA @921600 | — | **PARTIALLY VERIFIED** [D6] — the two-port split and the 921600 default are confirmed; the 115200 CLI rate is not |

**Detection gap found and fixed (2026-08-13).** The mmWave factories matched
only the TI vendor id, but per [D5] and [D6] the IWR6843ISK ships with **either**
an XDS110 **or** a SiLabs CP2105 depending on board revision. Half the boards in
the field would never have been detected, presenting as a sensor that is
silently absent rather than as an error. `UsbSerialTransport` now takes a *set*
of acceptable vendor ids and the filter declares both bridges.

## Wire formats

| claim | value | status |
|---|---|---|
| ~~DWM3000 shell commands~~ | `$INIT`, `$RANGE`, `$STOP` (CRLF) | **RESOLVED — it is ours.** Qorvo publishes no ASCII ranging protocol; stock DWM3001CDK firmware runs a CLI/UCI app over USB CDC with no `$RANGE`. Now documented as the **Aura anchor protocol** in `docs/uwb_anchor_protocol.md` and requires custom anchor firmware. |
| ~~DWM3000 range reply~~ | `ANCHOR-A=3.214,ANCHOR-B=7.882;CIR=0.42,1.87` | **RESOLVED — ours.** Specified in `docs/uwb_anchor_protocol.md`; implemented identically in the Python driver and `UwbGeometry.parseLine`. Still unverified against real firmware. |
| ~~NLOS marker~~ | trailing `*` on a range | **RESOLVED — ours.** Same document. |
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
