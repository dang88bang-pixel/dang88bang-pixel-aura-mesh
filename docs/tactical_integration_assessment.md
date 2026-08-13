# Tactical integration proposals — assessment

Four documents were supplied covering tactical AI, swarm/multi-agent
coordination, AR/VR, TAK/ATAK integration, Meshtastic, OpenStreetMap, USB
peripherals, training datasets, IEC 62443-4-2 certification, and a gap
analysis. This records which of it is real, which is already built, and which
does not apply — then implements the one item worth doing first.

## The short version

The proposals contain **one genuinely correct strategic call**, and it was
correctly identified as highest priority:

> "Der wichtigste Schritt für Ihr AURA-Projekt ist die Implementierung des
> Cursor-on-Target (CoT)-Protokolls."

That is right, for the reason given: CoT is the common language of the whole
TAK ecosystem, so speaking it buys interoperability with ATAK, iTAK, WinTAK,
TAKX and TAK Server at once, instead of building N plugins. It is also
hardware-independent — unlike most of the previous 3D proposal, nothing about
the CT45P blocks it. **It is now implemented.**

The rest divides into three piles: already built, misattributed, and blocked.

## 1. The gap analysis is about a different repository

The final document analyses **88P3dKart.-Art (3dxAgent v4.4.0)** and lists
modules as missing. That analysis does not describe this repository. Checked:

| Listed as missing | Status here |
| --- | --- |
| `ekf_fusion.py` — "Kernalgorithmus fehlt" | `aura/ekf.py`, 14 dedicated tests |
| `icp_merger.py` — "Punktwolken-Registrierung fehlt" | `ScanMatcher` in `aura/fusion.py` |
| `models.py` — "Datenmodelle fehlen" | 11 Pydantic models in `aura/api.py` |
| `pipeline.py` — "zentrale Pipeline fehlt" | `FusionPipeline` in `aura/fusion.py` |
| `pointcloud_compressor.py` | `aura/voxel.py` (178.5× measured) |
| `uwb_processor.py` — "UWB-Signalverarbeitung fehlt" | `aura/doppler.py` + `VitalsEstimator` |
| `mqtt_bridge.py` | genuinely absent — MQTT is optional in the spec |
| Web-Visualizer "keine 3D-Viewer-Logik" | 6 modules incl. `SceneManager.js` |
| Android "nur Build-Dateien, keine Kotlin-Quellen" | **24 Kotlin files**, ~4 250 lines |
| Tests "fehlen" | **195 passing**, plus 171 Kotlin and 618 C++ checks |
| CI/CD "fehlen" | `ci/github-actions-{ci,apk}.yml` |

The suggested replacement code would also be a regression. Its `AdaptiveEKF`
is a 6-state linear filter with `predict()` doing `0.5·a·t²` in the *body*
frame — no quaternion attitude, no gravity removal, no bias estimation, and
`adapt_covariance()` is an empty stub despite the class name. Our EKF is
15-state with gyro/accel bias estimation and is validated against a measured
drift baseline (`docs/open_issues_research.md`).

**Do not merge that analysis's recommendations.** It would delete working,
tested code and replace it with less capable code.

## 2. CoT export — built in this commit

`aura/cot.py` plus `GET /api/v1/agent/export/cot`.

### The problem nobody in the proposals mentioned

**CoT is absolutely geo-referenced. AURA's fusion output is not.**

The EKF works in a local metric frame whose origin is wherever the agent
started. CoT `<point>` requires WGS84 lat/lon. No amount of code derives one
from the other — someone must state where the local origin sits on Earth and
how local +x is rotated from true north.

Every proposal treats CoT export as a formatting exercise. It is not; that is
the whole difficulty. So `GeoAnchor` is a **required explicit input**, and
without it the endpoint returns **409** rather than emitting events. The
default of lat=0/lon=0 is in the Gulf of Guinea, and a tactical display would
draw contacts there without complaint.

```
$ curl localhost:8080/api/v1/agent/export/cot
HTTP 409
{"detail":"no geo anchor configured; set GEO_ANCHOR='lat,lon[,hae[,yaw]]'.
           AURA's local frame has no position on Earth until you do."}

$ AURA_GEO_ANCHOR="52.3759,9.7320,55.0,0.0" ...
HTTP 200
<event version="2.0" uid="AURA-…-self-0" type="a-f-G-U-C" how="m-g" …>
  <point lat="52.37596289" lon="9.73209150" hae="56.38" ce="0.06" le="0.21"/>
```

### Three decisions that matter more than the XML

**Detected people are never labelled friendly.** A radar return is a body, not
an allegiance. Contacts are emitted as `a-u-G` — atom, **unknown**
affiliation, ground. Tactical displays colour-code from affiliation, so
guessing here paints an unknown contact friendly or hostile on someone's map.
The self-position is `a-f-G-U-C` because we do know who we are.

**Accuracy is reported honestly.** CoT's `ce`/`le` are conventionally filled
with `9999999.0` ("unknown"). AURA actually knows its uncertainty, so `ce`
comes from the EKF covariance via the Rayleigh 95% factor (2.4477 for 2D) and
`le` from the vertical sigma via the 1D factor (1.96). Low-confidence
contacts get a proportionally larger `ce`, so a detection we are 10% sure of
does not arrive looking like a survey point.

**Stale times shrink as quality degrades** — 120 s good, 60 s degraded, 30 s
poor, 15 s lost. Over-long stale values leave ghost tracks on the common
operating picture after the source is gone, which is worse than no track.

### Scope, stated plainly

This module **produces** CoT. It does not open sockets, implement the TAK
Server streaming protocol, or do TLS enrolment. Transport is left to the
caller (file drop, UDP multicast 239.2.3.1:6969, TAK Server client,
Meshtastic). Generating correct XML has one right answer; transport depends on
deployment. **It has not been validated against a real ATAK client** — that
requires hardware we do not have, and XML that parses in isolation can still
fail strict ATAK validation on `detail` sub-elements.

### Two bugs this work surfaced

1. **Docstring contradicted code.** `to_wgs84` was documented as "x east-ish,
   y north-ish" while the rotation makes **+x north** at `yaw_deg=0`. Since
   `yaw_deg` is defined as the bearing of +x, the code was right. Fixed the
   docstring; tests now pin the convention.
2. **Wrong EKF field name.** The encoder first read `sigma_max`, which the EKF
   does not publish — it publishes `position_sigma = [sx, sy, sz]`. This
   failed *silently*: `None` → `ce=9999999.0`, i.e. valid-looking CoT that
   threw away the one thing the module exists for. Caught only by testing
   against the live agent, not by unit tests. `test_reads_the_real_ekf_field_name`
   now guards it, and the vertical sigma is used for `le` instead of discarded.

21 tests in `tests/test_cot.py`, including the geodesy checked against an
independent value (1° latitude ≈ 111 291 m at 52.4 N) rather than against
itself.

## 3. The rest, graded

| Proposal | Verdict | Basis |
| --- | --- | --- |
| **CoT export** | **done** | this commit |
| Meshtastic over USB-OTG | **feasible, recommended next** | LoRa module is external hardware; CT45P is host. ~5–10 kbit/s suits CoT events, not point clouds — the proposal says this correctly |
| Node-RED as CoT bridge | feasible, low effort | consumes the endpoint just built |
| Offline OSM (Organic Maps) | feasible | but see the geo-anchor problem: an indoor map needs anchoring before it can overlay |
| USB peripherals (BLE dongle, RTL-SDR, FTDI) | **already built** | `UsbSerialTransport.kt`, 7 whitelisted VID/PIDs incl. RTL-SDR `0BDA:2838` |
| Local LLM co-pilot | **already built** | `LLMService.kt`, Qwen2.5-1.5B Q4_K_M — unconstructed pending a GGUF |
| Audit logging / forensics | **already built** | `aura/audit.py`, SHA-256 hash chain, cross-platform digests pinned |
| AR overlay in tactical glasses | not on this hardware | needs ARCore; see `docs/3d_roadmap_assessment.md` §3–4 |
| Drone swarm command (Nemyx/MUM-T) | out of scope | no drone interface exists; this is a new product, not a feature |
| "97.2 % Lageerfassungsgenauigkeit" | unsourced | simulation figures with no cited paper. Compare our measured numbers in `performance_targets.md` |
| MARL "+30 % Win-Rate" | unsourced | wargame metric, not an indoor-positioning one |
| IEC 62443-4-2 certification | **not a code task** | it is an audit against an OT standard, costing money and time. Our audit chain and TEE key handling help, but no commit makes a product certified |
| Training datasets (HYMN, WALL-CLT, …) | plausible, unverified | not checked — none is needed for the current pipeline, which is analytic rather than learned. Note the proposal's own caveat that several need author contact |

## 4. On the "Datensouveränität" document

Its principles are right and largely match what is built (local-only storage,
no telemetry, hash-chained audit). Two corrections:

- The compliance table marks DSGVO, BSI TR-03180, VS-NfD, IEC 62443-4-2 and
  NIST SP 800-193 as **✅ erfüllt**. None of those can be self-declared. They
  are audits by accredited bodies. Claiming conformance in a repository is a
  liability, not a feature. What we can honestly say is *which technical
  controls exist*.
- "AES-256 gilt als quantenresistent" is loose. Grover's algorithm halves the
  effective key length, so AES-256 retains ~128-bit security — strong, and the
  standard recommendation, but the phrasing overstates it.

## 5. Recommendation

1. **Done: CoT export.** The correct first step, for the stated reason.
2. **Next: Meshtastic transport** over USB-OTG, carrying the CoT events this
   commit produces. That is the smallest step that makes AURA useful to a team
   rather than one operator, and the CT45P's USB host mode already works.
3. **Then: anchor acquisition UX.** The geo anchor is now the binding
   constraint on everything geo-referenced. GNSS gives a coarse anchor
   outdoors before entry; that flow needs designing, not just coding.
4. **Do not** action the 88P3dKart gap analysis against this repository.

*Verified 2026-08-13 against the CoT event schema (uid/type/time/start/stale,
point lat/lon/hae/ce/le), MIL-STD-2525 affiliation prefixes, and this
repository's actual contents.*
