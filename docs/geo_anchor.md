# The geo anchor — and why its accuracy is the number that matters

The CT45P hardware summary is largely accurate and most of it was already
assessed (see [`3d_roadmap_assessment.md`](3d_roadmap_assessment.md) and
[`tactical_integration_assessment.md`](tactical_integration_assessment.md)).
One line in it is genuinely actionable:

> **GPS/GNSS** ✅ L1/L5, Multi-GNSS — Georeferenzierung der 3D-Karten

That is right, and it closes the gap flagged as blocking two commits ago: the
geo anchor was environment-variable only, so CoT export required knowing the
coordinates *before* deployment. GNSS is the obvious way to get them on site.

Building it surfaced a **bug in the CoT export shipped in `6e884b8`**, which is
the more important half of this commit.

## The bug: the anchor's error was silently discarded

`ce` was computed from the EKF covariance alone. The EKF is genuinely good —
it reported **0.06 m** against the live agent. But that is accuracy *relative
to the local origin*. If the origin itself came from a handheld GNSS fix good
to 5 m, then every absolute position AURA publishes is a 5 m position.

The two errors are independent and add in quadrature:

| Anchor source | Anchor σ | Exported `ce` (was) | Exported `ce` (now) |
| --- | --- | --- | --- |
| Surveyed point | 0.00 m | 0.61 m | 0.61 m |
| GNSS, open sky | 1.50 m | 0.61 m ❌ | 3.72 m |
| GNSS, beside a building | 5.00 m | 0.61 m ❌ | 12.25 m |

The middle column was wrong by up to **20×**, in the dangerous direction: a
tactical display colour-codes and sizes markers from `ce`, so AURA was drawing
a confidently-precise marker on a position it did not actually know. This is
exactly the failure mode the CoT work set out to avoid — the commit message for
`6e884b8` says *"`ce`/`le` carry real accuracy instead of the conventional
9999999 junk"*, and then the anchor term was left out.

`GeoAnchor` now carries `sigma_m` and `source`, both propagate into `ce` for
self **and** contacts, and the anchor provenance is visible downstream:

```xml
<point lat="52.37595984" lon="9.73209175" ce="12.24" le="0.21"/>
<remarks>AURA fusion; quality=good; anchor=gnss +/-5.00m</remarks>
<aura quality="good" anchor_source="gnss" anchor_sigma_m="5.00"/>
```

A consumer can now distinguish a surveyed origin from a phone fix. Mutation
tested: reverting the propagation fails exactly 2 tests.

## Acquiring the anchor

`POST /api/v1/agent/geo/anchor` sets it at runtime and overrides the env var.
Intended flow: **stand outdoors where the sky is open, take a fix, then walk
in.** Indoors GNSS is either unavailable or — worse — confidently wrong from
multipath.

```
$ curl localhost:8080/api/v1/agent/export/cot
HTTP 409  "no geo anchor; POST /api/v1/agent/geo/anchor with a GNSS fix …"

$ curl -X POST localhost:8080/api/v1/agent/geo/anchor \
    -d '{"lat":52.3759,"lon":9.7320,"hae":55.0,"sigma_m":5.0,"source":"gnss"}'
HTTP 200

$ curl localhost:8080/api/v1/agent/export/cot
ce=12.24 m   anchor=gnss +/-5.00m
```

`sigma_m` defaults to a **pessimistic 10 m** rather than 0. A caller who omits
it must not silently get "perfect" — that is the bug above, reintroduced
through an API default.

## On the handheld

`GeoAnchorProvider.kt` reads GNSS through `LocationManager`. Three decisions
worth stating:

- **Never `getLastKnownLocation()`.** A cached fix from a different building
  returns instantly and looks fine. That is precisely the failure this class
  exists to prevent, so it always requests a fresh fix.
- **A fix with no stated accuracy is rejected**, not assigned a guess.
  Guessing defeats the purpose of carrying σ at all.
- **Altitude is passed through as-is.** Android reports height above the WGS84
  ellipsoid, which is what CoT `hae` wants. "Correcting" it to MSL is the
  classic source of tens of metres of vertical error.

The permission was already declared in the manifest; nothing read it.

### Magnetic declination

`yaw_deg` is the bearing of the local **+x** axis from **true** north. A
magnetometer reads *magnetic* north. Feeding that in directly rotates the
entire map by the local declination — about +4° in central Europe, which is
**~7 m of cross-track error at 100 m** and grows linearly with distance.
`GeoAnchorMath.trueBearing()` exists so the conversion is written down once;
callers must apply `GeomagneticField.getDeclination()` first.

## Testability

`GeoAnchorProvider` imports `LocationManager`, so it cannot run on the host
JVM. The parts that are easy to get quietly wrong are pure functions in
`GeoAnchorMath.kt` with **no Android imports**, compiled and run by
`tools/run-kotlin-tests.sh` (**24 new checks**): declination wrap-around, fix
acceptance gating, quadrature combination, and the `ce` conversion.

`sigmaToCe` uses the Rayleigh factor **2.4477**, matching `_sigma_to_ce` in
`aura/cot.py`. Both are pinned by tests; if one moves the other must move, or
handheld and agent will disagree about accuracy.

## Corrections to the hardware summary

Mostly accurate. Four points:

| Claim | Correction |
| --- | --- |
| "6 GB RAM … für Phi-3-mini (Q4_K_M, ~2,1 GB) ausreichend" | Two different figures are given for the same model (~2.1 GB and ~2.8 GB). Our pin is Qwen2.5-1.5B Q4_K_M, and `LLMService` remains **unconstructed** pending a GGUF — no on-device inference has been measured. |
| "Transformer-Inferenz" on 8 cores | The cores are **Kryo 260** (Snapdragon 660-class, 11 nm). Realistic for the analytic pipeline; not a basis for planning transformer inference. See `3d_roadmap_assessment.md` §1. |
| "Sensor-Suite … präzise, driftkorrigierte 3D-Kartierung **ohne externe Referenz**" | Measured drift without aiding: **60 s walking → 4777 m**. Inertial-only mapping is not viable at any accuracy; the aiding sensors are what make it work (`open_issues_research.md`). |
| Barometer → "Stockwerk-Erkennung" | Plausible and currently unused for that. Barometric pressure drifts with weather — a floor-detection feature needs a reference, which is a design task, not a sensor read. |

Genuinely useful and correctly identified: **GNSS L1/L5 for georeferencing**
(built here) and **USB-C OTG as the sensor interface** (already built).

## Status

- Implemented: anchor uncertainty propagation, `POST`/`GET /geo/anchor`,
  `GeoAnchorProvider`, `GeoAnchorMath` + 24 host-JVM checks.
- Not done: wiring the provider into a UI button, and any test on real GNSS
  hardware. The acquisition flow is untested on a device.

*Verified 2026-08-13 against the running agent, Android `LocationManager` /
`Location.getAccuracy()` semantics (68% horizontal radius), and this
repository's measured EKF output.*
