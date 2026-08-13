# Sensor fault detection — and a review of the "Smart UHAL Adapter" proposal

Two parts. First the proposal, which is written in Dart for a Flutter app and
targets an abstraction (`UHALInterface`) that does not exist in this repository
— and whose algorithms, transcribed and run, mostly do the opposite of what
their comments claim. Second the one real gap it points at, which is now fixed.

---

## Part 1 — the proposal

### 1.1 It targets a different codebase

```
$ find . -name '*.dart' -o -name 'pubspec.yaml'      # nothing
$ grep -rn 'UHAL' --include='*.kt' --include='*.py'  # nothing
```

No Dart, no Flutter, no `UHALInterface`, no `SafeBleUHAL`, no `BleException`,
no `FlutterBluePlus`. AURA's Android app is Kotlin; the edge agent is Python.
This is the second proposal in this session written against a codebase that is
not this one — see the same note in `docs/completion_plan.md`.

Exponential backoff with jitter, which the proposal presents as missing, ships
in three places already:

| file | what it does |
|---|---|
| `AgentApiClient.kt:178` | WS reconnect, 500 ms → 30 s cap, **plus jitter** so a squad of devices does not retry in lockstep |
| `web-visualizer/server.js:173` | bridge reconnect, 500 ms → 15 s cap |
| `web-visualizer/src/DataFetcher.js:71` | browser reconnect, 500 ms → 10 s cap |

### 1.2 Six defects, measured

Each algorithm was transcribed faithfully and executed. Language is irrelevant
to these; the logic is wrong.

**(a) The challenge-response authenticates nothing.** `_verifyChallengeResponse`
generates the challenge, computes the response, computes the *expected*
response, and compares them — all locally, with the same secret. The peer is
never contacted.

```
Auth succeeded in 1000/1000 runs
```

It returns true unconditionally. An attacker passes it too. This is not weak
authentication, it is the absence of authentication behind a name that suggests
otherwise. Worse, the code calls it "Zero-Knowledge", which it also is not.

**(b) The adaptive timeout is inverted.** `timeoutMs = base * _networkQuality`,
where quality falls as the link degrades:

| RSSI | quality | timeout |
|---|---|---|
| −30 dBm | 1.00 | 5000 ms |
| −70 dBm | 0.43 | 2143 ms |
| −95 dBm | 0.10 | **500 ms** |

A healthy link gets 5 s of patience; a barely-alive one gets 500 ms. Slow links
need *longer* timeouts. As written it guarantees the timeouts it exists to
prevent, and then the failure counter it feeds triggers a fallback — a
self-inflicted disconnect cascade.

**(c) The integrity check rejects every valid frame.** `_verifyDataIntegrity`
computes CRC16 over the whole buffer *including the two CRC bytes*, then
compares it against those same bytes.

```
Correctly-formed frames accepted: 0/10000
```

Every good packet is dropped. (CRC over data+CRC has a fixed residue; comparing
that residue to the transmitted CRC is simply a different check that no sender
satisfies.) The stream is dead on arrival.

**(d) The "compression" is lossy and undecodable.** RLE that emits `value`
followed by `count` only when `count > 1` cannot be parsed, because counts and
values share the byte space:

```
input [5,5,3] -> [5,2,3]
input [5,2,3] -> [5,2,3]      identical encoding
```

On 200 bytes of sensor data it achieves **200 → 200 bytes**. The result is then
transmitted to a receiver that was never told about RLE.

**(e) The predictor is a constant.** `predictFailure` reads
`_rssiHistory[connectionId] ?? RingBuffer(20)` — the fallback buffer is never
stored back into the map, and nothing anywhere calls `.add()`. The history is
permanently empty, so every prediction uses the defaults (`rssi=-80`,
`latency=100`, `error=0`). Also `RingBuffer.lastOrNull` is declared as a method
but used as a property, and `PredictionResult` is a class declared *inside*
another class body, which Dart does not permit. The file would not compile.

**(f) The health monitor overruns itself.** `Timer.periodic(5 s)` calls a check
whose worst case is `3 × 5 s timeout + 3 s backoff = 18 s`. Timers do not wait
for the previous run, so ~3.6 checks overlap under exactly the degraded
conditions they are meant to measure — each one calling `connect()` while
`sendCommand` concurrently switches paths on the same socket, with no lock.

### 1.3 The fallback architecture does not fit this hardware

The proposal treats BLE / WiFi / UART / NFC as interchangeable routes to one
destination. In AURA they are not routes; they are different sensors:

| sensor | transport | load | can it fail over? |
|---|---|---|---|
| RPLIDAR | USB serial 115200 | ~8 kB/s point cloud | no — it is a cable |
| IWR6843 | USB serial 921600 | ~40 kB/s TLV | no — it is a cable |
| BLE tokens | BLE advertising | RSSI, ~20 B | no — that is the radio standard |
| IMU | internal to the SoC | 200 Hz | no — it is inside the device |

A USB LiDAR cannot "fall back to NFC". And NFC as a data path is ~4 cm range
with no Android streaming profile — unusable for a belt-worn sensor feed.

Where a genuine alternate path *does* exist — the CT45P reaching the edge agent
over WLAN — the backoff reconnect above already covers it.

---

## Part 2 — the real gap, now closed

Underneath the proposal is one correct instinct: **a sensor can fail without
saying so.** AURA had a blind spot here, and it was worse than nothing.

### 2.1 What was missing

`SensorStatus` (`aura/sensors/base.py`) answers *"did a frame arrive
recently?"* via `age_seconds` and `healthy`. That catches an unplugged cable:
`read()` returns None, age grows, healthy goes false.

It does not catch a **wedged** sensor — firmware hung, DMA buffer being
re-read, driver replaying its last good frame. Frames keep arriving on
schedule. Measured by freezing the UWB driver while keeping its bookkeeping
truthful:

```
connected = True     healthy = True
age_secs  = 0.037    samples = 251
```

Indistinguishable from a healthy sensor. And a Kalman filter treats every
repeat of a value as independent confirmation, so the damage compounds:

| | position error | reported sigma | overconfidence |
|---|---|---|---|
| **without detection** | 0.46 m | 0.086 m | **5.4×** |
| **with detection** | 0.22 m | 0.528 m | 0.4× |

Sigma *shrank* as error grew. That is the same class of bug as the geo-anchor
defect in `acd57a4`: a number that looks confident and is not.

A second finding while investigating: `mahalanobis_gate()` in `ekf.py:509` is
defined, documented, given a threshold — and **never called from anywhere**.
The two `rejected_updates` sites use ad-hoc checks instead. Left in place for
now, but it is dead code pretending to be a safety net.

### 2.2 What shipped

`edge-agent/aura/sensor_health.py`:

- **`StuckDetector`** — fingerprints each payload and counts consecutive
  repeats. Default threshold 40 (4 s at 10 Hz). A gap (`None`) deliberately
  does *not* reset the count, or an intermittently wedged sensor would never
  trip it.
- **`RateMonitor`** — measured Hz against promised Hz over a 50-sample window;
  starved below 50 %. Silent until it has ≥3 samples, so it cannot cry wolf at
  startup.
- **`SensorHealthMonitor`** — per-driver verdicts: `ok`, `degraded`, `stuck`,
  `stale`.
- **`inflate_sigma`** — `degraded` triples the noise; `stuck`/`stale` return
  `inf`, meaning *skip the update*. There is no finite sigma that makes a
  constant reading informative.

Wired into `FusionPipeline.tick()` for all six drivers. A non-usable verdict
skips that sensor's EKF update and increments `rejected_updates`. `state()` now
merges the verdict over the driver's own status, so a frozen-but-connected
sensor **cannot report itself healthy**:

```json
"uwb": { "healthy": false, "health": "stuck",
         "health_reason": "payload unchanged for 40 samples (4.0 s)" }
```

### 2.3 Why the threshold is safe

Real sensors are noisy, so identical consecutive frames are essentially
impossible. Measured over 300 reads per driver:

| driver | distinct frames | largest repeat run |
|---|---|---|
| lidar / mmwave / uwb / ble / imu / thermal | **300 / 300** each | **1** |

Zero repeats in 1800 reads against a threshold of 40. Confirmed live: 1236
iterations, six drivers `ok`, no false positives.

### 2.4 What was deliberately not built

- **Failure prediction from RSSI trends.** Indoor multipath swings RSSI ~20 dB
  over one step sideways; a linear fit over 20 samples extrapolates noise. We
  report faults that have begun, with evidence, rather than guessing at ones
  that have not.
- **Automatic transport failover.** As shown above, AURA's transports are not
  interchangeable.
- **A second authentication layer.** Bearer REST + `?token=` WS already ship.
  Adding the proposal's challenge-response would have *reduced* security to
  zero while appearing to raise it.

Coverage: 25 tests in `edge-agent/tests/test_sensor_health.py`, including an
end-to-end freeze against the live pipeline. Removing the fusion guard fails
that test.
