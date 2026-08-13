"""Passive bistatic radar: cross-ambiguity function and clutter cancellation.

A passive radar exploits an existing illuminator (DVB-T, DAB, FM, LTE). Two
receive channels are needed:

* the **reference** channel, pointed at the transmitter (clean copy of ``s``),
* the **surveillance** channel, watching the scene (echoes + direct leakage).

Targets appear as peaks of the cross-ambiguity function (CAF)::

    chi(tau, f) = sum_n  surv[n] * conj(ref[n - tau]) * exp(-j 2 pi f n / fs)

Direct-path leakage is typically 60-90 dB stronger than any echo, so the CAF
is useless until the direct signal and static clutter are projected out --
that is what :func:`eca_cancel` (Extensive Cancellation Algorithm) does.

Resolution reality check
------------------------
Range resolution is ``c / (2 * B)``. An RTL-SDR's 2.4 MHz of usable bandwidth
gives ~62 m, not the <10 m sometimes quoted; a full 8 MHz DVB-T channel with
a wideband SDR gives ~19 m. Velocity resolution is ``lambda / (2 * T_int)``.
See ``docs/performance_targets.md``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

SPEED_OF_LIGHT = 299_792_458.0


@dataclass
class RangeDopplerMap:
    """CAF magnitude over (bistatic range, Doppler)."""

    magnitude: np.ndarray                 # shape (doppler_bins, range_bins)
    range_bins: np.ndarray                # metres of bistatic range
    doppler_bins: np.ndarray              # Hz
    sample_rate: float
    carrier_hz: float
    integration_time: float

    @property
    def shape(self) -> tuple[int, int]:
        return self.magnitude.shape

    def to_db(self, floor_db: float = -80.0) -> np.ndarray:
        peak = float(self.magnitude.max())
        if peak <= 0:
            return np.full_like(self.magnitude, floor_db)
        with np.errstate(divide="ignore"):
            db = 20.0 * np.log10(np.maximum(self.magnitude / peak, 1e-12))
        return np.maximum(db, floor_db)

    def velocity_bins(self) -> np.ndarray:
        """Doppler in Hz -> bistatic velocity in m/s."""
        wavelength = SPEED_OF_LIGHT / self.carrier_hz
        return self.doppler_bins * wavelength / 2.0

    def as_dict(self, decimate: int = 1) -> dict:
        step = max(1, decimate)
        db = self.to_db()[::step, ::step]
        return {
            "range_bins": [round(float(v), 2) for v in self.range_bins[::step]],
            "doppler_bins": [round(float(v), 3) for v in self.doppler_bins[::step]],
            "velocity_bins": [round(float(v), 3) for v in self.velocity_bins()[::step]],
            "magnitude_db": [[round(float(v), 2) for v in row] for row in db],
            "shape": list(db.shape),
            "integration_time": self.integration_time,
        }


@dataclass
class RadarDetection:
    """A CAF peak that survived CFAR."""

    bistatic_range: float     # metres
    doppler_hz: float
    velocity: float           # m/s, bistatic
    snr_db: float
    range_bin: int
    doppler_bin: int

    def as_dict(self) -> dict:
        return {
            "bistatic_range": round(self.bistatic_range, 2),
            "doppler_hz": round(self.doppler_hz, 3),
            "velocity": round(self.velocity, 3),
            "snr_db": round(self.snr_db, 2),
            "range_bin": self.range_bin,
            "doppler_bin": self.doppler_bin,
        }


# ----------------------------------------------------------------------
# resolution helpers
# ----------------------------------------------------------------------
def range_resolution(bandwidth_hz: float) -> float:
    """``c / (2B)`` -- the physical limit, no processing can beat it."""
    return SPEED_OF_LIGHT / (2.0 * max(bandwidth_hz, 1.0))


def velocity_resolution(carrier_hz: float, integration_time: float) -> float:
    """``lambda / (2*T)`` -- longer dwell buys finer Doppler."""
    wavelength = SPEED_OF_LIGHT / max(carrier_hz, 1.0)
    return wavelength / (2.0 * max(integration_time, 1e-9))


def unambiguous_velocity(carrier_hz: float, prf_hz: float) -> float:
    wavelength = SPEED_OF_LIGHT / max(carrier_hz, 1.0)
    return wavelength * prf_hz / 4.0


def doppler_resolution(integration_time: float) -> float:
    """Smallest resolvable Doppler shift: ``1 / T``.

    This is a hard limit set by the dwell, and it bites in practice: a 16k
    sample record at 2.4 MSps lasts 6.8 ms and therefore cannot separate
    anything closer than ~146 Hz. :func:`compute_caf` will happily *evaluate*
    the CAF at a 70 Hz grid point, but that is interpolation inside one
    resolution cell, not resolving power -- to actually resolve 70 Hz you need
    T > 14 ms (n > 34k samples at 2.4 MSps).
    """
    return 1.0 / max(integration_time, 1e-12)


def required_samples_for_doppler(target_hz: float, sample_rate: float) -> int:
    """How many samples a dwell needs to resolve ``target_hz``."""
    if target_hz <= 0:
        return 0
    return int(math.ceil(sample_rate / target_hz))


# ----------------------------------------------------------------------
# clutter cancellation
# ----------------------------------------------------------------------
def eca_cancel(
    surveillance: np.ndarray,
    reference: np.ndarray,
    num_taps: int = 32,
    regularisation: float = 1e-6,
) -> np.ndarray:
    """Extensive Cancellation Algorithm (least-squares direct-path removal).

    Builds a delay basis from the reference channel and projects the
    surveillance channel onto its orthogonal complement, removing the direct
    path *and* zero-Doppler multipath in one shot::

        s_clean = s - X (X^H X + eps I)^-1 X^H s
    """
    surv = np.asarray(surveillance, dtype=complex).ravel()
    ref = np.asarray(reference, dtype=complex).ravel()
    n = min(len(surv), len(ref))
    surv, ref = surv[:n], ref[:n]
    taps = max(1, min(num_taps, n))

    # X[:, k] = reference delayed by k samples
    X = np.zeros((n, taps), dtype=complex)
    for k in range(taps):
        X[k:, k] = ref[: n - k]

    gram = X.conj().T @ X + regularisation * np.eye(taps)
    try:
        weights = np.linalg.solve(gram, X.conj().T @ surv)
    except np.linalg.LinAlgError:  # pragma: no cover
        weights = np.linalg.lstsq(gram, X.conj().T @ surv, rcond=None)[0]
    return surv - X @ weights


def cancellation_ratio_db(before: np.ndarray, after: np.ndarray) -> float:
    """How much power the canceller removed, in dB."""
    p_before = float(np.mean(np.abs(np.asarray(before)) ** 2))
    p_after = float(np.mean(np.abs(np.asarray(after)) ** 2))
    if p_after <= 1e-30 or p_before <= 1e-30:
        return 0.0
    return 10.0 * math.log10(p_before / p_after)


# ----------------------------------------------------------------------
# cross-ambiguity function
# ----------------------------------------------------------------------
def compute_caf(
    surveillance: np.ndarray,
    reference: np.ndarray,
    sample_rate: float,
    max_range_bins: int = 64,
    doppler_bins: int = 129,
    max_doppler_hz: float = 200.0,
    carrier_hz: float = 626e6,
    batch_decimation: int = 1,
) -> RangeDopplerMap:
    """Batch-processed CAF.

    For each range lag the reference is delayed, mixed with the surveillance
    signal and Fourier-transformed along slow time. The FFT over the *whole*
    correlation product is what makes this tractable: a direct double loop
    over (lag, Doppler) would be O(N * lags * dopplers).
    """
    surv = np.asarray(surveillance, dtype=complex).ravel()
    ref = np.asarray(reference, dtype=complex).ravel()
    n = min(len(surv), len(ref))
    surv, ref = surv[:n], ref[:n]
    if n < 8:
        raise ValueError("need at least 8 samples to form a CAF")

    max_range_bins = max(1, min(max_range_bins, n - 1))
    decim = max(1, int(batch_decimation))

    # Doppler grid: an explicit DFT over the (decimated) product keeps the
    # frequency axis independent of the record length, which makes the map
    # comparable across integration times.
    doppler = np.linspace(-max_doppler_hz, max_doppler_hz, doppler_bins)
    magnitude = np.zeros((doppler_bins, max_range_bins))

    idx = np.arange(0, n, decim)
    phase_base = -2j * np.pi * idx / sample_rate
    # (doppler_bins, len(idx)) mixing matrix
    mixer = np.exp(phase_base[None, :] * doppler[:, None])

    for lag in range(max_range_bins):
        delayed = np.zeros(n, dtype=complex)
        if lag == 0:
            delayed = ref
        else:
            delayed[lag:] = ref[:-lag]
        product = (surv * np.conj(delayed))[idx]
        magnitude[:, lag] = np.abs(mixer @ product)

    integration_time = n / sample_rate
    range_axis = np.arange(max_range_bins) * SPEED_OF_LIGHT / sample_rate
    return RangeDopplerMap(
        magnitude=magnitude,
        range_bins=range_axis,
        doppler_bins=doppler,
        sample_rate=sample_rate,
        carrier_hz=carrier_hz,
        integration_time=integration_time,
    )


def compute_caf_fft(
    surveillance: np.ndarray,
    reference: np.ndarray,
    sample_rate: float,
    max_range_bins: int = 64,
    num_batches: int = 64,
    carrier_hz: float = 626e6,
) -> RangeDopplerMap:
    """Faster CAF via the batches algorithm (correlate, then FFT slow time).

    The record is split into ``num_batches`` segments; each segment is
    correlated at every lag, and an FFT across segments yields Doppler. This
    is O(batches * lags * batch_len + lags * batches log batches) and is the
    variant the C++/NEON port implements.
    """
    surv = np.asarray(surveillance, dtype=complex).ravel()
    ref = np.asarray(reference, dtype=complex).ravel()
    n = min(len(surv), len(ref))
    surv, ref = surv[:n], ref[:n]
    num_batches = max(2, min(num_batches, n // 4))
    batch_len = n // num_batches
    if batch_len < 2:
        raise ValueError("record too short for the requested number of batches")
    max_range_bins = max(1, min(max_range_bins, batch_len - 1))

    corr = np.zeros((num_batches, max_range_bins), dtype=complex)
    for b in range(num_batches):
        s0 = b * batch_len
        s1 = s0 + batch_len
        sb = surv[s0:s1]
        rb = ref[s0:s1]
        for lag in range(max_range_bins):
            if lag == 0:
                corr[b, lag] = np.vdot(rb, sb)
            else:
                corr[b, lag] = np.vdot(rb[:-lag], sb[lag:])

    spectrum = np.fft.fftshift(np.fft.fft(corr, axis=0), axes=0)
    magnitude = np.abs(spectrum)
    batch_rate = sample_rate / batch_len
    doppler = np.fft.fftshift(np.fft.fftfreq(num_batches, d=1.0 / batch_rate))
    range_axis = np.arange(max_range_bins) * SPEED_OF_LIGHT / sample_rate
    return RangeDopplerMap(
        magnitude=magnitude,
        range_bins=range_axis,
        doppler_bins=doppler,
        sample_rate=sample_rate,
        carrier_hz=carrier_hz,
        integration_time=n / sample_rate,
    )


# ----------------------------------------------------------------------
# detection
# ----------------------------------------------------------------------
def cfar_2d(
    magnitude: np.ndarray,
    guard: tuple[int, int] = (2, 2),
    training: tuple[int, int] = (6, 6),
    threshold_db: float = 12.0,
) -> np.ndarray:
    """Cell-averaging CFAR. Returns a boolean detection mask.

    Each cell is compared against the mean power of a training ring that
    excludes a guard band, so a strong target cannot raise its own threshold.
    """
    mag = np.asarray(magnitude, dtype=float)
    power = mag ** 2
    rows, cols = power.shape
    gj, gi = guard
    tj, ti = training
    mask = np.zeros_like(power, dtype=bool)
    linear_threshold = 10.0 ** (threshold_db / 10.0)

    for j in range(rows):
        j0, j1 = max(0, j - tj), min(rows, j + tj + 1)
        gj0, gj1 = max(0, j - gj), min(rows, j + gj + 1)
        for i in range(cols):
            i0, i1 = max(0, i - ti), min(cols, i + ti + 1)
            gi0, gi1 = max(0, i - gi), min(cols, i + gi + 1)
            window = power[j0:j1, i0:i1]
            guard_block = power[gj0:gj1, gi0:gi1]
            total = float(window.sum()) - float(guard_block.sum())
            count = window.size - guard_block.size
            if count <= 0:
                continue
            noise = total / count
            if noise <= 1e-30:
                continue
            if power[j, i] > linear_threshold * noise:
                mask[j, i] = True
    return mask


def detect_targets(
    rd_map: RangeDopplerMap,
    threshold_db: float = 12.0,
    max_targets: int = 10,
    exclude_zero_doppler: float = 0.5,
) -> list[RadarDetection]:
    """CFAR + peak picking, skipping the zero-Doppler clutter ridge."""
    mask = cfar_2d(rd_map.magnitude, threshold_db=threshold_db)
    power = rd_map.magnitude ** 2
    noise_floor = float(np.median(power[power > 0])) if np.any(power > 0) else 1e-12
    velocities = rd_map.velocity_bins()

    candidates: list[RadarDetection] = []
    rows, cols = mask.shape
    for j in range(rows):
        if abs(rd_map.doppler_bins[j]) < exclude_zero_doppler:
            continue
        for i in range(cols):
            if not mask[j, i]:
                continue
            # local maximum test (3x3)
            j0, j1 = max(0, j - 1), min(rows, j + 2)
            i0, i1 = max(0, i - 1), min(cols, i + 2)
            if power[j, i] < power[j0:j1, i0:i1].max() - 1e-18:
                continue
            snr = 10.0 * math.log10(max(power[j, i], 1e-30) / max(noise_floor, 1e-30))
            candidates.append(
                RadarDetection(
                    bistatic_range=float(rd_map.range_bins[i]),
                    doppler_hz=float(rd_map.doppler_bins[j]),
                    velocity=float(velocities[j]),
                    snr_db=snr,
                    range_bin=i,
                    doppler_bin=j,
                )
            )
    candidates.sort(key=lambda d: d.snr_db, reverse=True)
    return candidates[:max_targets]


# ----------------------------------------------------------------------
# bistatic geometry
# ----------------------------------------------------------------------
def bistatic_ellipse_point(
    tx: tuple[float, float],
    rx: tuple[float, float],
    bistatic_range: float,
    angle: float,
) -> tuple[float, float] | None:
    """Point on the iso-range ellipse for a given bistatic range and bearing.

    A single passive receiver localises a target only to an ellipse with foci
    at the transmitter and receiver; a bearing (from an array) or a second
    receiver is required to pin down a point.
    """
    baseline = math.dist(tx, rx)
    total = baseline + bistatic_range          # |T->P| + |P->R|
    if total <= baseline:
        return None
    a = total / 2.0
    c = baseline / 2.0
    b2 = a * a - c * c
    if b2 <= 0:
        return None
    b = math.sqrt(b2)
    cx = (tx[0] + rx[0]) / 2.0
    cy = (tx[1] + rx[1]) / 2.0
    rotation = math.atan2(rx[1] - tx[1], rx[0] - tx[0])
    ex = a * math.cos(angle)
    ey = b * math.sin(angle)
    return (
        cx + ex * math.cos(rotation) - ey * math.sin(rotation),
        cy + ex * math.sin(rotation) + ey * math.cos(rotation),
    )


# ----------------------------------------------------------------------
# signal generation (simulation + tests)
# ----------------------------------------------------------------------
def generate_ofdm_reference(
    num_samples: int,
    sample_rate: float = 2.4e6,
    num_carriers: int = 512,
    rng: np.random.Generator | None = None,
) -> np.ndarray:
    """Noise-like OFDM waveform standing in for a DVB-T illuminator.

    DVB-T is effectively Gaussian noise, which is exactly what gives passive
    radar its thumbtack ambiguity function.
    """
    rng = rng or np.random.default_rng(0)
    symbols = (rng.choice([-1, 1], size=(num_samples // num_carriers + 1, num_carriers))
               + 1j * rng.choice([-1, 1], size=(num_samples // num_carriers + 1, num_carriers)))
    waveform = np.fft.ifft(symbols, axis=1).ravel()[:num_samples]
    power = np.sqrt(np.mean(np.abs(waveform) ** 2))
    return waveform / max(power, 1e-12)


def simulate_surveillance(
    reference: np.ndarray,
    sample_rate: float,
    targets: list[tuple[int, float, float]],
    direct_path_gain: float = 1.0,
    noise_sigma: float = 0.0,
    rng: np.random.Generator | None = None,
) -> np.ndarray:
    """Build a surveillance channel: direct path + delayed/Doppler echoes.

    ``targets`` entries are ``(delay_samples, doppler_hz, amplitude)``.
    """
    ref = np.asarray(reference, dtype=complex).ravel()
    n = len(ref)
    out = direct_path_gain * ref.astype(complex).copy()
    t = np.arange(n) / sample_rate
    for delay, doppler, amplitude in targets:
        delay = int(delay)
        echo = np.zeros(n, dtype=complex)
        if delay <= 0:
            echo = ref.copy()
        elif delay < n:
            echo[delay:] = ref[:-delay]
        out = out + amplitude * echo * np.exp(2j * np.pi * doppler * t)
    if noise_sigma > 0:
        rng = rng or np.random.default_rng(1)
        out = out + (rng.normal(0, noise_sigma, n) + 1j * rng.normal(0, noise_sigma, n)) / math.sqrt(2)
    return out
