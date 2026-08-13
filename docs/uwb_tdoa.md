# UWB TDoA — what it can locate, and what it cannot

This document answers a proposal that asked for two things:

1. UWB integration via the **Android UWB API** on the CT45P, and
2. **"Triangulation unbekannter Netzwerke"** — locating unknown devices by TDoA.

Both rest on premises that are false. They are corrected here first, because
the rest of the design only makes sense once they are out of the way. The
solver itself (`edge-agent/aura/tdoa.py`) is real, tested, and shipped — but it
solves a narrower problem than the proposal assumed.

---

## 1. The CT45P has no UWB radio

> *"CT45P unterstützt UWB über die Android UWB API ab Android 12."*

It does not. This was already recorded as **VERIFIED** in this repository before
the proposal arrived:

| where | what it already said |
|---|---|
| `docs/source_claims.md:47` | "CT45P has no UWB radio — not listed. **VERIFIED**. The radio list is WWAN / WLAN / Bluetooth 5.1 + BLE / NFC / GPS. No UWB, on any SKU." |
| `docs/uwb_anchor_protocol.md:74` | the DWM3000-over-serial protocol exists precisely because there is no on-board radio |
| `UwbManager.kt` (file header) | states the same, at the top of the source file |

Two independent blockers, either of which is sufficient:

| blocker | requirement | CT45P-X0N |
|---|---|---|
| hardware | a UWB transceiver | **absent from every SKU** |
| API level | `androidx.core.uwb` needs **API 31+** | ships **Android 11 = API 30** (`minSdk 30`) |

`androidx.core.uwb` on this device would compile and then find no radio at
runtime. The only route to UWB on a CT45P is the one already built: an external
**DWM3000 over USB-serial**, speaking AURA's own protocol
(`docs/uwb_anchor_protocol.md`). Everything below assumes that hardware.

## 2. TDoA cannot find "unknown networks"

> *"Triangulation unbekannter Netzwerke."*

TDoA is not a surveillance primitive. The mechanism is:

1. a **tag transmits a UWB blink** on the agreed channel and preamble,
2. each anchor timestamps the arrival,
3. a central engine turns the arrival-time *differences* into hyperbolas and
   intersects them.

Step 1 requires a **cooperating transmitter**. A device that does not emit a
UWB blink produces no arrival times, and there is nothing to difference. TDoA
therefore locates *your own tags*. It cannot enumerate or position arbitrary
unknown devices.

Locating non-cooperating people or objects is a genuinely different problem,
and this repo already addresses it by other means — see
`docs/rf_reconstruction.md`:

| goal | wrong tool | right tool (already shipped) |
|---|---|---|
| locate our own UWB tag | — | **TDoA / TWR** |
| detect a person carrying nothing | TDoA | **mmWave IWR6843**, RTI |
| detect a moving person through a wall | TDoA | **RTI** (`aura/rti.py`), measured 1–2 m |
| enumerate nearby phones | TDoA | **BLE scan** (identifiers, not geometry) |

A related caveat: BLE/Wi-Fi MAC randomisation means even the BLE path
enumerates *advertisements*, not stable devices.

---

## 3. The binding constraint is anchor clock sync

This is the part no proposal mentions, and it decides whether TDoA is worth
building at all.

TDoA multiplies clock error by the speed of light:

```
1 ns of anchor clock offset  =  29.98 cm of range-difference error
```

The error enters the position **directly**; it is not averaged away, because a
constant offset is a systematic bias, not noise. Required sync for a given
accuracy target:

| position target | required anchor sync |
|---|---|
| 0.10 m | **334 ps** |
| 0.30 m | **1.0 ns** |
| 1.00 m | **3.3 ns** |

And what real sync sources deliver:

| sync source | typical offset | resulting range error | verdict |
|---|---|---|---|
| NTP over Wi-Fi | ~1 ms | **~300 km** | useless — not "less accurate", useless |
| PTP / IEEE 1588 software | ~100 ns | ~30 m | useless indoors |
| GPS-disciplined PPS | ~20 ns | ~6 m | worse than the BLE fallback |
| **wired backbone / DW3000 in-band sync** | ~0.1 ns | **~0.03 m** | the only workable option |

Measured against the shipped solver (4 anchors, 10 × 8 m room, 200 trials):

| anchor sync | mean position error |
|---|---|
| ±0.1 ns | **0.02 m** |
| ±1.0 ns | **0.21 m** |
| ±20 ns | **diverges** (~1e17 m unconstrained) |

That last row is the dangerous one. At 20 ns the hyperbolas no longer intersect,
and an unconstrained Gauss-Newton solve runs off to ~1e17 m — a number that is
still a pair of floats, and would still render as a map marker. The solver
therefore caps each step at 50 m, gates the solution radius at
`MAX_SOLUTION_RADIUS_M = 1000`, and **returns `None`** rather than a fix.

### Why TWR is the default here

TWR (two-way ranging) needs **no anchor sync at all** — the round-trip
cancels the offset, and double-sided TWR cancels drift as well. Its cost is
throughput: each tag must have a scheduled exchange with each anchor, so
concurrent tag capacity is limited.

| | TWR (shipped, `UwbGeometry.trilaterate`) | TDoA (`aura/tdoa.py`) |
|---|---|---|
| anchor sync | **not required** | sub-nanosecond, wired |
| tag capacity | limited by the ranging schedule | very high (one blink each) |
| tag power | transmits and receives | transmits only |
| infrastructure | anchors need no backbone | anchors need a **wired** backbone |

**TWR remains the default.** TDoA is worth its cabling only for many tags at
once. For AURA's handful of tokens, TWR wins — which is why trilateration
shipped first and TDoA is the addition, not the replacement.

---

## 4. What shipped

`edge-agent/aura/tdoa.py`:

- `TdoaSolver(anchors, sync_sigma_ns, range_sigma_m)` — Gauss-Newton on range
  differences. `sync_sigma_ns` is a **required** argument with no default:
  assuming perfect clocks is exactly the mistake this module exists to prevent.
- `sync_to_range_sigma(ns)` — the 30 cm/ns conversion, used for the reported σ.
- `TdoaFix` — `x`, `y`, `sigma_m`, `residual_m`, `anchors_used`, `iterations`,
  `gdop`.
- Refusals: fewer than 3 anchors (construction error), fewer than 2 differences,
  singular/collinear geometry, GDOP > 20, non-finite input, divergence.

`POST /api/v1/agent/uwb/tdoa` — returns the fix plus `usable_sync`,
`sync_range_sigma_m`, and a `warning`.

**When sync is unusable the fix is withheld, not merely flagged.** It reappears
under `diagnostic_only_fix`, a name nothing will render by accident. The
reasoning is the geo-anchor bug from `acd57a4`: an honest σ is no protection
against a caller that plots position and ignores σ.

Live behaviour, 4 anchors, transmitter at (3.5, 5.5):

| `sync_sigma_ns` | `fix` | reported σ | `usable_sync` |
|---|---|---|---|
| 0.1 | (3.50, 5.50) | 0.12 m | true |
| 1.0 | (3.50, 5.50) | 0.37 m | true |
| 20.0 | **withheld** | 6.93 m | false |
| 1 000 000 | **withheld** | 346 km | false |

Coverage: 20 tests in `edge-agent/tests/test_tdoa.py` + 5 API tests. The
sync→error table above is pinned by test, so the documented numbers cannot
drift from the code.

### A bug the tests caught

The first implementation inferred the reference anchor as "the first anchor
present in the measurement dict". But the reference is by construction the one
anchor **absent** from that dict — differences are measured *against* it. The
solver therefore promoted a measured anchor to reference and silently discarded
one difference, reducing a 4-anchor solve to 2 differences and shifting the fix
by ~31 cm on noise-free input. It converged, reported a small residual, and was
wrong. Reverting the fix fails 7 tests.

---

## 5. Not built, and why

| item | status |
|---|---|
| Android UWB API path | **impossible** on CT45P — no radio, API 30 |
| DWM3000 TDoA firmware | not written; needs hardware AURA does not have |
| anchor sync backbone | not built; the prerequisite, not a detail |
| 3D (z) TDoA | solver is 2D; z needs anchors at differing heights and a GDOP that indoor ceilings rarely give |
| Single-Anchor-Multipath localisation | **research-stage.** Published results assume a known room geometry and a trained multipath fingerprint. Not deployable. |
| passive detection of unknown transmitters | out of scope for TDoA — see `docs/rf_reconstruction.md` |

The honest summary: the solver is correct and tested, and it will stay unused
until someone builds a wired, sub-nanosecond-synchronised anchor array. On the
CT45P, with a handful of tokens, **TWR is the right answer and is already
shipped**.
