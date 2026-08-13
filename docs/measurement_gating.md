# Measurement gating — rejecting bad data without starving the filter

`mahalanobis_gate()` had been sitting in `ekf.py` since the first commit:
defined, documented as *"used to reject outlier measurements before fusing"*,
given a threshold — and **never called from anywhere**. A grep across the whole
repository returned only its own definition.

That is worse than not having it. The docstring asserts a safety property the
code does not provide, and the two `rejected_updates` counters in `fusion.py`
made it look as though gating was happening. It was not.

---

## 1. What the missing gate cost

### 1.1 A single bad range is permanent

Injecting one corrupted UWB range into a converged filter, then running 200
clean ticks:

| injected range | position error after 200 ticks | reported σ | quality |
|---|---|---|---|
| +0 m (control) | 0.13 m | 0.090 | good |
| +10 m | 0.15 m | 0.089 | good |
| +50 m | 1.26 m | 0.084 | good |
| **+200 m** | **8.29 m** | **0.092** | **good** |

The filter never recovers. It reports σ ≈ 0.09 m — *smaller* than before the
outlier — while sitting 8 m from truth. Ten consecutive +50 m readings (a
plausible multipath cluster) left a 3.78 m error, also labelled `good`.

This is the geo-anchor bug from `acd57a4` and the frozen-sensor bug from
`ad5a443` for the third time: **a number that looks confident and is not.**

### 1.2 A single NaN destroys the filter outright

`aura/sensors/uwb.py` parsed hardware ranges with a bare `float()`. Python
accepts rather more than the author intended:

```
"3.214"  -> 3.214        "inf"    -> inf
"nan"    -> nan          "1e400"  -> inf
"-5.0"   -> -5.0         "99999"  -> 99999.0
```

One corrupted serial line — a dropped byte, a noisy USB connector — and:

| injected | result |
|---|---|
| `inf` | **crash**: `ValueError: math domain error` from `wrap_pi` |
| `nan` | **crash**: `cannot convert float NaN to integer`; state becomes `(nan, nan)`, quality `lost`, permanently |
| `1e9` | no crash — operator placed at **(−207268, 278949)**, quality `good` |
| `-5.0` | no crash, silently fused |

The `1e9` row is the dangerous one: no exception, no warning, a position
280 km away, and a `good` label on it.

---

## 2. The fix, at two layers

### 2.1 Driver: bound what a range may be

`MAX_PLAUSIBLE_RANGE_M = 300.0` — DW1000/DW3000 hardware tops out near 300 m in
free space and far less indoors. Non-finite and out-of-range values are counted
in `status.errors` and dropped at the parse boundary:

```
input : A=3.21, B=inf, C=nan, D=-5.0, E=1e400, F=99999, G=7.5
kept  : {A: 3.21, G: 7.5}        errors: 5
```

### 2.2 Filter: gate every update

Both guards live in `_update()`, which every sensor funnels through:

- **Non-finite rejection** on `z`, `h`, `H` and `R`.
- **Chi-square gate** on the *innovation* covariance `S = H P Hᵀ + R`, not on
  `P`. `S` is what says how surprising a measurement should be given both the
  state uncertainty and the sensor's own noise.

Threshold 16.0 = 4σ at one degree of freedom, which scales itself per sensor:

| sensor | σ | accepted deviation |
|---|---|---|
| UWB (LOS) | 0.12 m | 0.48 m |
| UWB (NLOS) | 0.55 m | 2.20 m |
| BLE | 1.40 m | 5.60 m |

A walker covers ~0.15 m per tick at 10 Hz and the prediction follows the
motion, so honest innovations stay well inside 0.48 m.

### 2.3 Results after the fix

| injected | error after 200 ticks | σ | crash | rejected |
|---|---|---|---|---|
| `inf` | 0.17 m | 0.096 | no | 1 |
| `nan` | 0.16 m | 0.095 | no | 1 |
| `1e9` | 0.14 m | 0.093 | no | 1 |
| +200 m | 0.17 m | 0.095 | no | 1 |
| +50 m | 0.18 m | 0.095 | no | 1 |

No false positives: 900 healthy ticks, **7179 updates, 0 rejections**, mean
error 0.142 m against the project baseline of 0.157 m. Live agent: 3953
updates, 0 rejections.

---

## 3. The trap: a gate that starves its own filter

The first implementation broke `test_uwb_range_trilateration_converges`. The
temptation was to raise the threshold until it passed — 25.0 would have done
it. That would have been tuning to the test.

The actual behaviour, from a cold start 5.8 m off truth:

```
it0  innov=+5.548  sqrt(S)=2.001  d2=  7.69  ok
it0  innov=-0.435  sqrt(S)=0.093  d2= 21.69  REJECTED
it1  innov=-0.521  sqrt(S)=0.067  d2= 61.20  REJECTED
it5  innov=-0.560  sqrt(S)=0.063  d2= 77.77  REJECTED
```

The filter grows *confident* faster than it grows *accurate*. Once σ collapses
to 0.06 m, every honest measurement looks impossible, so it is gated away,
which keeps σ small. **359 consecutive rejections**, converging to 0.36 m off
with high confidence. A gate that permanently locks out the measurements that
would fix the estimate is not outlier rejection; it is starvation.

The fix: after `max_consecutive_rejects` (5) in a row, the filter concludes the
*estimate* is wrong rather than the sensor, and lets one update through with
`R` inflated 100× so it nudges rather than yanks.

### 3.1 Why the counter must be per source

The first attempt at the escape hatch used a single global counter and changed
nothing — the streak never exceeded 2. Two of the four anchors were locked out
permanently while the other two were accepted, and every acceptance reset the
shared counter.

```
Streaks per source: {'uwb:0.0,9.0': 120, 'altitude': 120, 'uwb:0.0,0.0': 119}
```

Counting per measurement source fixes it: cold-start error 0.36 m → **0.14 m**,
with outlier protection intact (a +200 m range still moves the estimate 0.0000 m).

Those streaks of ~120 are worth stating plainly: this filter *is* chronically
overconfident in the UWB-only cold-start case, and the escape hatch is
carrying it. The gate did not cause that; it exposed it. Fixing the underlying
covariance tuning is open work.

---

## 4. Parity with the C++ filter

`aura_core.cpp` carries a port of the same EKF for the NDK path, and it had the
identical defects — verified by direct probe:

```
converged:      (5.999, 4.000)
after +200 m:   (6.336, 4.415)      <- 0.4 m jump
after NaN:      (nan, nan)          <- permanent
```

Both guards, the escape hatch and the per-source streak table are now mirrored
there (`kMaxSources = 64`, anchor position hashed so each anchor keeps its own
streak). Post-fix: converges to (5.996, 3.994), and both attacks are refused.

An observation that only appeared in the C++ test: run without `predict()`
between update batches, the healthy case rejects **665 of 800** updates; with
prediction it rejects **160**. A gate tuned against update-only sequences would
be tuned against a fiction — the tests now call `predict()` as the real loop
does.

---

## 5. Honest note on redundancy

The explicit non-finite check in `_update()` is **not load-bearing on its own**.
`mahalanobis_gate` refuses non-finite input independently of the threshold, so
deleting the explicit check leaves every test green — in both Python and C++,
confirmed by mutation.

It is kept because it states the invariant where a reader looks for it, and
because it does not depend on the gate staying as it is. But it is recorded
here rather than left to look like a second line of defence that has been
verified. The invariant itself — *no non-finite measurement may reach the
state* — is pinned by test regardless of which guard enforces it.

---

## 6. Coverage

| suite | count |
|---|---|
| `edge-agent/tests/test_ekf_gating.py` | 21 |
| `edge-agent/tests/test_sensors.py` (UWB parser bounds) | +4 |
| `cpp/tests/test_aura_core.cpp` (`testEkfGating`) | +16 |

Mutation-verified: removing the chi-square gate fails 5 Python and 3 C++
checks; making the streak counter global fails 1.

`rejected` is exposed on `/api/v1/agent/state` under `ekf`, alongside
`updates`.
