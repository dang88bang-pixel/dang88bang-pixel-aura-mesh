"""UWB micro-Doppler analysis: respiration and heart-rate extraction.

Chest-wall displacement phase-modulates the UWB channel impulse response.
Sampling the CIR tap at ~20 Hz and running a windowed FFT reveals a peak at
0.15-0.6 Hz (9-36 breaths/min) and a weaker one at 0.8-2.5 Hz (48-150 bpm).

Everything is implemented on top of ``numpy.fft`` so SciPy stays optional.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

import numpy as np

RESP_BAND = (0.12, 0.65)     # Hz  -> 7..39 breaths/min
HEART_BAND = (0.80, 2.60)    # Hz  -> 48..156 bpm


@dataclass
class VitalSigns:
    """Result of one micro-Doppler analysis window."""

    respiration_hz: float = 0.0
    respiration_bpm: float = 0.0
    heart_hz: float = 0.0
    heart_bpm: float = 0.0
    respiration_snr: float = 0.0
    heart_snr: float = 0.0
    presence: bool = False
    confidence: float = 0.0
    samples: int = 0
    sample_rate: float = 0.0

    def as_dict(self) -> dict:
        return {
            "respiration_hz": round(self.respiration_hz, 4),
            "respiration_bpm": round(self.respiration_bpm, 2),
            "heart_hz": round(self.heart_hz, 4),
            "heart_bpm": round(self.heart_bpm, 2),
            "respiration_snr": round(self.respiration_snr, 2),
            "heart_snr": round(self.heart_snr, 2),
            "presence": self.presence,
            "confidence": round(self.confidence, 3),
            "samples": self.samples,
            "sample_rate": round(self.sample_rate, 2),
        }


def hann(n: int) -> np.ndarray:
    """Hann window (avoids depending on ``scipy.signal``)."""
    if n <= 1:
        return np.ones(max(n, 1))
    return 0.5 - 0.5 * np.cos(2.0 * np.pi * np.arange(n) / (n - 1))


def detrend(x: np.ndarray) -> np.ndarray:
    """Remove a linear trend (breathing sits on top of slow platform drift)."""
    n = len(x)
    if n < 3:
        return x - float(np.mean(x)) if n else x
    t = np.arange(n, dtype=float)
    A = np.column_stack([t, np.ones(n)])
    coeffs, *_ = np.linalg.lstsq(A, x, rcond=None)
    return x - A @ coeffs


def band_peak(freqs: np.ndarray, power: np.ndarray, band: tuple[float, float]) -> tuple[float, float]:
    """Return ``(peak_frequency, snr)`` inside a band, 0/0 when empty."""
    mask = (freqs >= band[0]) & (freqs <= band[1])
    if not np.any(mask):
        return (0.0, 0.0)
    sub = power[mask]
    sub_freqs = freqs[mask]
    idx = int(np.argmax(sub))
    peak_power = float(sub[idx])
    # parabolic interpolation for sub-bin resolution
    peak_freq = float(sub_freqs[idx])
    if 0 < idx < len(sub) - 1:
        y0, y1, y2 = sub[idx - 1], sub[idx], sub[idx + 1]
        denom = y0 - 2 * y1 + y2
        if abs(denom) > 1e-18:
            delta = 0.5 * (y0 - y2) / denom
            step = float(sub_freqs[1] - sub_freqs[0]) if len(sub_freqs) > 1 else 0.0
            peak_freq += float(np.clip(delta, -0.5, 0.5)) * step
    noise = float(np.median(power[power > 0])) if np.any(power > 0) else 1e-12
    snr = 10.0 * math.log10(max(peak_power, 1e-18) / max(noise, 1e-18))
    return (peak_freq, snr)


class MicroDopplerAnalyzer:
    """Sliding-window vital-sign estimator fed with CIR amplitude/phase.

    Threshold calibration (measured, see ``tests/test_sensors.py``):

    * pure sensor noise  -> respiration-band SNR mean 4.9 dB, p95 8.7 dB
    * a real 0.28 Hz chest wall -> 33-35 dB

    The bands are therefore separated by ~25 dB, and the default 18 dB gate
    sits comfortably between them. An earlier 6 dB gate produced a **33 %
    false-positive rate on pure noise** - the system would report a person
    breathing behind a wall in an empty building, which is far worse than
    missing a weak return.

    ``presence_hits`` additionally requires the detection to persist across
    several consecutive windows, killing isolated noise spikes.
    """

    def __init__(
        self,
        window: int = 256,
        min_samples: int = 64,
        presence_snr_db: float = 18.0,
        presence_hits: int = 3,
    ) -> None:
        self.window = window
        self.min_samples = min_samples
        self.presence_snr_db = presence_snr_db
        self.presence_hits = presence_hits
        self._samples: deque[float] = deque(maxlen=window)
        self._times: deque[float] = deque(maxlen=window)
        self._consecutive = 0
        self.last: VitalSigns = VitalSigns()

    def push(self, timestamp: float, amplitude: float, phase: float = 0.0) -> None:
        """Add one CIR observation. Phase (when available) is more sensitive."""
        value = phase if abs(phase) > 1e-9 else amplitude
        self._samples.append(float(value))
        self._times.append(float(timestamp))

    def reset(self) -> None:
        self._samples.clear()
        self._times.clear()
        self._consecutive = 0
        self.last = VitalSigns()

    @property
    def sample_rate(self) -> float:
        if len(self._times) < 2:
            return 0.0
        span = self._times[-1] - self._times[0]
        if span <= 1e-6:
            return 0.0
        return (len(self._times) - 1) / span

    def analyze(self) -> VitalSigns:
        n = len(self._samples)
        fs = self.sample_rate
        if n < self.min_samples or fs < 2.0:
            self.last = VitalSigns(samples=n, sample_rate=fs)
            return self.last

        x = detrend(np.asarray(self._samples, dtype=float))
        x = x * hann(n)
        # zero-pad to 4x for a finer frequency grid
        nfft = 1 << int(math.ceil(math.log2(max(n * 4, 256))))
        spectrum = np.fft.rfft(x, n=nfft)
        power = np.abs(spectrum) ** 2
        freqs = np.fft.rfftfreq(nfft, d=1.0 / fs)
        power[freqs < 0.05] = 0.0  # kill DC/drift

        resp_hz, resp_snr = band_peak(freqs, power, RESP_BAND)
        heart_hz, heart_snr = band_peak(freqs, power, HEART_BAND)

        # Temporal persistence: a single window above the gate is not enough.
        # Breathing is periodic and lasts, noise spikes do not.
        if resp_snr >= self.presence_snr_db:
            self._consecutive = min(self._consecutive + 1, self.presence_hits * 2)
        else:
            self._consecutive = 0
        presence = self._consecutive >= self.presence_hits
        confidence = float(np.clip((resp_snr - self.presence_snr_db) / 12.0, 0.0, 1.0))

        self.last = VitalSigns(
            respiration_hz=resp_hz if presence else 0.0,
            respiration_bpm=resp_hz * 60.0 if presence else 0.0,
            heart_hz=heart_hz if presence and heart_snr > 3.0 else 0.0,
            heart_bpm=heart_hz * 60.0 if presence and heart_snr > 3.0 else 0.0,
            respiration_snr=resp_snr,
            heart_snr=heart_snr,
            presence=presence,
            confidence=confidence,
            samples=n,
            sample_rate=fs,
        )
        return self.last


@dataclass
class ThroughWallDetection:
    """A person detected behind an obstruction."""

    detection_id: str
    x: float
    y: float
    range_m: float
    bearing: float
    respiration_bpm: float
    confidence: float
    behind_wall: bool
    timestamp: float

    def as_dict(self) -> dict:
        return {
            "detection_id": self.detection_id,
            "x": round(self.x, 3),
            "y": round(self.y, 3),
            "range": round(self.range_m, 3),
            "bearing": round(self.bearing, 4),
            "respiration_bpm": round(self.respiration_bpm, 1),
            "confidence": round(self.confidence, 3),
            "behind_wall": self.behind_wall,
            "timestamp": self.timestamp,
        }


class ThroughWallTracker:
    """Fuses mmWave targets + UWB vitals into stable through-wall tracks."""

    def __init__(self, gate: float = 1.4, max_age: float = 4.0) -> None:
        self.gate = gate
        self.max_age = max_age
        self._tracks: dict[str, dict] = {}
        self._next_id = 1

    def update(
        self,
        observations: list[tuple[float, float, bool]],
        vitals: VitalSigns,
        timestamp: float,
    ) -> list[ThroughWallDetection]:
        """``observations`` are ``(x, y, behind_wall)`` in the map frame."""
        for x, y, behind in observations:
            best_id, best_dist = None, self.gate
            for tid, track in self._tracks.items():
                d = math.hypot(track["x"] - x, track["y"] - y)
                if d < best_dist:
                    best_id, best_dist = tid, d
            if best_id is None:
                best_id = f"twd-{self._next_id:03d}"
                self._next_id += 1
                self._tracks[best_id] = {"x": x, "y": y, "hits": 0}
            track = self._tracks[best_id]
            track["x"] = 0.6 * track["x"] + 0.4 * x
            track["y"] = 0.6 * track["y"] + 0.4 * y
            track["behind_wall"] = behind
            track["last_seen"] = timestamp
            track["hits"] = track.get("hits", 0) + 1

        stale = [tid for tid, tr in self._tracks.items() if timestamp - tr.get("last_seen", 0) > self.max_age]
        for tid in stale:
            del self._tracks[tid]

        out: list[ThroughWallDetection] = []
        for tid, track in self._tracks.items():
            if track.get("hits", 0) < 2:
                continue
            out.append(
                ThroughWallDetection(
                    detection_id=tid,
                    x=track["x"],
                    y=track["y"],
                    range_m=math.hypot(track["x"], track["y"]),
                    bearing=math.atan2(track["y"], track["x"]),
                    respiration_bpm=vitals.respiration_bpm,
                    confidence=min(1.0, 0.25 * track.get("hits", 0)) * max(0.35, vitals.confidence),
                    behind_wall=bool(track.get("behind_wall", False)),
                    timestamp=track.get("last_seen", timestamp),
                )
            )
        return out
