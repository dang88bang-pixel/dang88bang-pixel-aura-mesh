"""Detecting the sensor faults that do not announce themselves.

`SensorStatus` already answers "did a frame arrive recently?". That catches an
unplugged cable, which is the easy case: `read()` returns None, `age_seconds`
grows, `healthy` goes false.

It does not catch the failure that matters more. A serial sensor whose firmware
has wedged, a DMA buffer that is being re-read, a driver replaying its last good
frame -- all keep delivering frames on time. `age_seconds` stays near zero and
`healthy` stays true while the values have been constant for minutes. Measured
on this pipeline: freezing the UWB driver left `healthy=True`, `age=0.037 s`,
`samples=251`, and roughly doubled position error while sigma *shrank*, because
a Kalman filter treats each repeat of a value as independent confirmation.

This module adds the two checks that catch it:

* :class:`StuckDetector` -- has the payload actually changed?
* :class:`RateMonitor`   -- is it arriving at the rate it promised?

Both are deliberately dumb and cheap. They run per driver per tick, so anything
clever would cost more than the fusion step it guards.

What this is *not*: a predictor. Trend-extrapolating RSSI to forecast a
disconnect sounds attractive and does not survive contact with indoor
multipath, where RSSI swings 20 dB over one step sideways. We report faults
that have already begun, with evidence, and let the caller decide.
"""

from __future__ import annotations

import hashlib
import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class HealthState(str, Enum):
    """Ordered worst-last so ``max()`` picks the most severe."""

    OK = "ok"
    DEGRADED = "degraded"
    STUCK = "stuck"
    STALE = "stale"

    @property
    def severity(self) -> int:
        return {"ok": 0, "degraded": 1, "stuck": 2, "stale": 3}[self.value]


# A sensor may legitimately repeat a value: a stationary LiDAR facing a blank
# wall, a thermal frame in a still room. The threshold has to be high enough
# that normal quiet does not trip it, low enough that a wedged driver is caught
# within a few seconds. At 10 Hz, 40 repeats is 4 s.
DEFAULT_STUCK_REPEATS = 40

# Rate has to miss badly before we complain -- a scheduler hiccup is not a
# fault. Half the promised rate, sustained over the window, is.
DEFAULT_RATE_TOLERANCE = 0.5


def _digest(payload: Any) -> str:
    """Order-insensitive-enough fingerprint of a driver payload.

    `repr()` is used rather than JSON because payloads here are dataclasses,
    numpy arrays and dicts in a mix that json cannot take without a custom
    encoder. We only need "did this change", not a canonical form.
    """
    try:
        raw = repr(payload)
    except Exception:  # pragma: no cover - defensive; repr should not raise
        return "<unrepresentable>"
    return hashlib.blake2b(raw.encode("utf-8", "replace"), digest_size=16).hexdigest()


@dataclass
class StuckDetector:
    """Flags a payload that has stopped changing.

    Counts consecutive identical fingerprints. A single repeat is meaningless;
    `threshold` consecutive repeats is a wedged sensor.
    """

    threshold: int = DEFAULT_STUCK_REPEATS
    _last: str | None = field(default=None, repr=False)
    repeats: int = 0
    first_repeat_ts: float = 0.0

    def observe(self, payload: Any, now: float | None = None) -> bool:
        """Feed one sample. Returns True while the sensor looks stuck."""
        if payload is None:
            # No frame is the *other* failure mode; RateMonitor owns it.
            # Do not let a gap reset the repeat count, or an intermittent
            # wedged sensor would never reach the threshold.
            return self.is_stuck

        now = time.time() if now is None else now
        fingerprint = _digest(payload)
        if fingerprint == self._last:
            if self.repeats == 0:
                self.first_repeat_ts = now
            self.repeats += 1
        else:
            self._last = fingerprint
            self.repeats = 0
            self.first_repeat_ts = 0.0
        return self.is_stuck

    @property
    def is_stuck(self) -> bool:
        return self.repeats >= self.threshold

    def stuck_seconds(self, now: float | None = None) -> float:
        if not self.is_stuck or not self.first_repeat_ts:
            return 0.0
        return max(0.0, (time.time() if now is None else now) - self.first_repeat_ts)

    def reset(self) -> None:
        self._last = None
        self.repeats = 0
        self.first_repeat_ts = 0.0


@dataclass
class RateMonitor:
    """Compares observed sample rate against what the driver promised."""

    expected_hz: float
    window: int = 50
    tolerance: float = DEFAULT_RATE_TOLERANCE
    _stamps: list[float] = field(default_factory=list, repr=False)

    def observe(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self._stamps.append(now)
        if len(self._stamps) > self.window:
            del self._stamps[: -self.window]

    @property
    def measured_hz(self) -> float | None:
        """None until there is enough history to mean anything."""
        if len(self._stamps) < 3:
            return None
        span = self._stamps[-1] - self._stamps[0]
        if span <= 0:
            return None
        return (len(self._stamps) - 1) / span

    @property
    def starved(self) -> bool:
        rate = self.measured_hz
        if rate is None or self.expected_hz <= 0:
            return False
        return rate < self.expected_hz * self.tolerance

    def reset(self) -> None:
        self._stamps.clear()


@dataclass
class HealthVerdict:
    """What we concluded about one driver, and the evidence for it."""

    name: str
    state: HealthState
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        """Whether the fusion loop should trust this driver's samples."""
        return self.state is HealthState.OK

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "state": self.state.value,
            "usable": self.usable,
            "reason": self.reason,
            **self.detail,
        }


class SensorHealthMonitor:
    """Per-driver stuck + rate checking.

    Usage is one call per driver per tick::

        verdict = monitor.observe("uwb", payload, expected_hz=10.0)
        if not verdict.usable:
            ...  # skip the fusion update, tell the operator

    Deliberately holds no reference to the drivers: it is fed samples, so it
    stays testable without hardware and cannot itself wedge a driver.
    """

    def __init__(self, stuck_threshold: int = DEFAULT_STUCK_REPEATS) -> None:
        self.stuck_threshold = stuck_threshold
        self._stuck: dict[str, StuckDetector] = {}
        self._rates: dict[str, RateMonitor] = {}
        self._gap_since: dict[str, float] = {}

    def observe(
        self,
        name: str,
        payload: Any,
        expected_hz: float = 0.0,
        stale_after: float = 2.0,
        now: float | None = None,
    ) -> HealthVerdict:
        now = time.time() if now is None else now
        stuck = self._stuck.setdefault(name, StuckDetector(self.stuck_threshold))

        if payload is None:
            started = self._gap_since.setdefault(name, now)
            gap = now - started
            if gap >= stale_after:
                return HealthVerdict(
                    name, HealthState.STALE,
                    f"no sample for {gap:.1f} s (limit {stale_after:.1f} s)",
                    {"gap_seconds": round(gap, 3)},
                )
            return HealthVerdict(
                name, HealthState.DEGRADED, f"gap of {gap:.1f} s",
                {"gap_seconds": round(gap, 3)},
            )

        self._gap_since.pop(name, None)
        stuck.observe(payload, now=now)

        if expected_hz > 0:
            rate = self._rates.setdefault(name, RateMonitor(expected_hz))
            rate.expected_hz = expected_hz
            rate.observe(now=now)
        else:
            rate = self._rates.get(name)

        if stuck.is_stuck:
            return HealthVerdict(
                name, HealthState.STUCK,
                f"payload unchanged for {stuck.repeats} samples "
                f"({stuck.stuck_seconds(now):.1f} s)",
                {
                    "repeats": stuck.repeats,
                    "stuck_seconds": round(stuck.stuck_seconds(now), 3),
                },
            )

        if rate is not None and rate.starved:
            measured = rate.measured_hz or 0.0
            return HealthVerdict(
                name, HealthState.DEGRADED,
                f"{measured:.1f} Hz against an expected {rate.expected_hz:.1f} Hz",
                {"measured_hz": round(measured, 3), "expected_hz": rate.expected_hz},
            )

        detail: dict[str, Any] = {"repeats": stuck.repeats}
        if rate is not None and rate.measured_hz is not None:
            detail["measured_hz"] = round(rate.measured_hz, 3)
        return HealthVerdict(name, HealthState.OK, "nominal", detail)

    def verdicts(self) -> dict[str, HealthVerdict]:
        """Last known state for every driver seen so far."""
        out: dict[str, HealthVerdict] = {}
        for name, stuck in self._stuck.items():
            if stuck.is_stuck:
                out[name] = HealthVerdict(
                    name, HealthState.STUCK,
                    f"payload unchanged for {stuck.repeats} samples",
                    {"repeats": stuck.repeats},
                )
            else:
                out[name] = HealthVerdict(name, HealthState.OK, "nominal",
                                          {"repeats": stuck.repeats})
        return out

    def worst(self) -> HealthState:
        states = [v.state for v in self.verdicts().values()]
        return max(states, key=lambda s: s.severity) if states else HealthState.OK

    def reset(self, name: str | None = None) -> None:
        if name is None:
            self._stuck.clear()
            self._rates.clear()
            self._gap_since.clear()
            return
        self._stuck.pop(name, None)
        self._rates.pop(name, None)
        self._gap_since.pop(name, None)


def inflate_sigma(base_sigma: float, verdict: HealthVerdict) -> float:
    """Noise to use for a suspect driver, if you fuse it at all.

    Dropping a stuck sensor outright is usually right. Where the geometry
    needs it -- range-only UWB with two anchors goes unobservable if you drop
    one -- fusing it with honest noise beats fusing it with a lie. `inf` means
    the caller must skip the update; there is no finite sigma that makes a
    constant reading informative.
    """
    if not math.isfinite(base_sigma) or base_sigma <= 0:
        raise ValueError("base_sigma must be finite and positive")
    if verdict.state is HealthState.OK:
        return base_sigma
    if verdict.state is HealthState.DEGRADED:
        return base_sigma * 3.0
    return math.inf
