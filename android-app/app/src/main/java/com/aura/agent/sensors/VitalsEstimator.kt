package com.aura.agent.sensors

import kotlin.math.abs
import kotlin.math.cos
import kotlin.math.hypot
import kotlin.math.sin

/**
 * Respiration and heart rate from the UWB channel-impulse-response amplitude.
 *
 * A chest wall moves roughly 4-12 mm when breathing and 0.2-0.5 mm with each
 * heartbeat. Neither changes the *range* measurably — both phase-modulate a
 * static multipath tap, which shows up as a slow oscillation in the CIR
 * amplitude of a fixed range bin. Feed that series here.
 *
 * Pure logic, no Android, no coroutines: this is the same split as
 * [UwbGeometry] and for the same reason — the estimator decides whether a
 * casualty is reported as breathing, so it has to be testable on a host JVM.
 *
 * ## Why a Goertzel filter rather than an FFT
 *
 * Only two narrow bands matter (0.1-0.6 Hz and 0.8-2.0 Hz). A Goertzel
 * evaluates one bin at a time at a fraction of the cost, needs no power-of-two
 * padding, and lets the frequency grid be chosen freely — an FFT over a 30 s
 * window at 20 Hz gives 0.033 Hz bins, i.e. 2 bpm granularity, which is
 * coarser than the clinical difference between 12 and 14 breaths/min.
 *
 * ## What this deliberately does not do
 *
 * It does not claim a heart rate whenever one is numerically extractable. The
 * cardiac component is ~20x weaker than respiration and sits near its second
 * and third harmonics; without a confidence gate the estimator would report a
 * "heart rate" that is simply a harmonic of the breathing. See [HARMONIC_GUARD].
 */
object VitalsEstimator {

    /** Respiration search band, Hz (6-36 breaths/min). */
    const val RESP_MIN_HZ = 0.10f
    const val RESP_MAX_HZ = 0.60f

    /** Cardiac search band, Hz (48-120 bpm). */
    const val HEART_MIN_HZ = 0.80f
    const val HEART_MAX_HZ = 2.00f

    /**
     * A candidate must exceed the in-band median by this factor to be
     * reported. Chosen from the same reasoning as the micro-Doppler presence
     * threshold: near the noise floor the false-positive rate is unacceptable
     * for a search-and-rescue readout.
     */
    const val MIN_SNR = 4.0f

    /**
     * Half-width of the harmonic mask, in natural FFT bins.
     *
     * A Hann window has a main lobe about 4 bins wide, so +/-2 bins removes
     * the lobe and nothing more. This is expressed in bins rather than as a
     * percentage on purpose: a percentage is the wrong shape. Measured over
     * plausible breathing rates, a +/-12% mask blocks **81-100%** of the
     * 48-120 bpm band — at 12 breaths/min it blocks the band entirely, so no
     * pulse could ever be reported — while the same percentage is *too narrow*
     * to cover the lobe at the low end of the band.
     */
    const val HARMONIC_MASK_BINS = 2.0f

    /**
     * A candidate must reach this share of the strongest masked harmonic's
     * power to be believed.
     *
     * Masking the main lobe is not sufficient. A strong harmonic also throws
     * sidelobes that are genuine local maxima outside the mask (measured: a
     * 104.9 peak at 0.90 Hz leaves a 2.8 bump at 0.939 Hz), and those would
     * otherwise be reported as a ~56 bpm pulse for a subject who has none.
     * Sidelobes of a Hann window sit far below this floor; a real pulse does
     * not.
     */
    const val MIN_HARMONIC_PROMINENCE = 0.15f

    /**
     * A near-harmonic peak is only rejected if the harmonic itself carries at
     * least this share of the fundamental's power.
     *
     * Proximity alone is not evidence. Sinusoidal breathing produces almost no
     * harmonic content (measured: power 0.001 at 5f0 versus 26.98 for a real
     * 1.15 Hz pulse), whereas non-sinusoidal chest motion produces strong ones
     * (30-105 at 2f0/3f0/5f0). Testing whether the harmonic is actually
     * *present* separates the two cases; testing frequency alone cannot.
     */
    const val HARMONIC_POWER_RATIO = 0.05f

    /** Shortest window that can resolve the low end of the respiration band. */
    const val MIN_SAMPLES = 64

    data class Vitals(
        val respirationBpm: Float?,
        val heartRateBpm: Float?,
        val respirationSnr: Float,
        val heartSnr: Float,
    ) {
        val hasSubject: Boolean get() = respirationBpm != null
    }

    /**
     * Estimate vitals from a CIR amplitude series.
     *
     * @param samples amplitude of the tracked range bin, oldest first
     * @param sampleRateHz rate the series was captured at
     */
    fun estimate(samples: FloatArray, sampleRateHz: Float): Vitals {
        if (samples.size < MIN_SAMPLES || sampleRateHz <= 0f) {
            return Vitals(null, null, 0f, 0f)
        }

        // Remove DC and linear drift. Body sway and thermal drift in the radio
        // produce a ramp far larger than the respiration signal; leaving it in
        // puts a huge component at ~0 Hz that leaks across the whole band.
        val detrended = detrend(samples)
        // A Hann window keeps that leakage from swamping the cardiac band,
        // which is ~20 dB below respiration.
        applyHann(detrended)

        val resolution = sampleRateHz / detrended.size
        // Never search below what the window length can resolve.
        val respLow = maxOf(RESP_MIN_HZ, resolution)

        val (respHz, respPower, respMedian) = peakInBand(
            detrended, sampleRateHz, respLow, RESP_MAX_HZ,
        )
        val respSnr = if (respMedian > 0f) respPower / respMedian else 0f
        val respiration = if (respSnr >= MIN_SNR && respHz > 0f) respHz else null

        // Search the cardiac band while *excluding* the neighbourhood of any
        // strong respiration harmonic.
        //
        // Rejecting after the fact is not enough: with non-sinusoidal chest
        // motion the harmonic is frequently the tallest peak in the band
        // (measured: 29.98 at 1.25 Hz versus 26.98 for a real 1.15 Hz pulse),
        // so a post-hoc test discards the peak and reports no pulse at all -
        // on a patient who has one. Excluding those bins during the search
        // lets the genuine second-place peak win.
        val binHz = sampleRateHz / detrended.size
        val excluded = if (respiration != null) {
            val fundamental = goertzelPower(detrended, sampleRateHz, respiration)
            (2..8).map { respiration * it }
                .filter { it >= HEART_MIN_HZ - 0.1f && it <= HEART_MAX_HZ + 0.1f }
                .filter { h ->
                    fundamental > 0f &&
                        goertzelPower(detrended, sampleRateHz, h) / fundamental >= HARMONIC_POWER_RATIO
                }
        } else {
            emptyList()
        }

        val (heartHz, heartPower, heartMedian) = peakInBand(
            detrended, sampleRateHz, HEART_MIN_HZ, HEART_MAX_HZ,
            excluded, HARMONIC_MASK_BINS * binHz,
        )
        val heartSnr = if (heartMedian > 0f) heartPower / heartMedian else 0f

        // Reject sidelobe artefacts of a masked harmonic.
        val strongestHarmonic = excluded
            .maxOfOrNull { goertzelPower(detrended, sampleRateHz, it) } ?: 0f
        val prominent = strongestHarmonic <= 0f ||
            heartPower / strongestHarmonic >= MIN_HARMONIC_PROMINENCE

        val heart: Float? =
            if (heartSnr >= MIN_SNR && heartHz > 0f && prominent) heartHz else null

        return Vitals(
            respirationBpm = respiration?.let { it * 60f },
            heartRateBpm = heart?.let { it * 60f },
            respirationSnr = respSnr,
            heartSnr = heartSnr,
        )
    }

    /** Strongest frequency in a band, its power, and the in-band median power. */
    private fun peakInBand(
        signal: FloatArray,
        sampleRateHz: Float,
        lowHz: Float,
        highHz: Float,
        excludeHz: List<Float> = emptyList(),
        maskHalfWidthHz: Float = 0f,
    ): Triple<Float, Float, Float> {
        if (lowHz >= highHz) return Triple(0f, 0f, 0f)
        // Oversample the frequency grid 4x relative to the natural resolution:
        // the Goertzel is cheap and it avoids scalloping loss when the true
        // rate falls between bins.
        val step = (sampleRateHz / signal.size) / 4f
        if (step <= 0f) return Triple(0f, 0f, 0f)

        var bestHz = 0f
        var bestPower = 0f
        val powers = ArrayList<Float>()

        val grid = ArrayList<Pair<Float, Float>>()   // (frequency, power)
        var f = lowHz
        while (f <= highHz) {
            val p = goertzelPower(signal, sampleRateHz, f)
            grid.add(f to p)
            powers.add(p)
            f += step
        }

        for ((freq, power) in grid) {
            if (excludeHz.any { abs(freq - it) <= maskHalfWidthHz }) continue
            if (power > bestPower) {
                bestPower = power
                bestHz = freq
            }
        }
        if (powers.isEmpty()) return Triple(0f, 0f, 0f)
        powers.sort()
        val median = powers[powers.size / 2]
        return Triple(bestHz, bestPower, median)
    }

    /** Power at one frequency, via the Goertzel algorithm. */
    fun goertzelPower(signal: FloatArray, sampleRateHz: Float, freqHz: Float): Float {
        val omega = 2.0 * Math.PI * freqHz / sampleRateHz
        val coeff = 2.0 * cos(omega)
        var s1 = 0.0
        var s2 = 0.0
        for (x in signal) {
            val s0 = x + coeff * s1 - s2
            s2 = s1
            s1 = s0
        }
        val real = s1 - s2 * cos(omega)
        val imag = s2 * sin(omega)
        return hypot(real, imag).toFloat()
    }

    /** Subtract the least-squares line. Returns a new array. */
    fun detrend(samples: FloatArray): FloatArray {
        val n = samples.size
        var sumX = 0.0; var sumY = 0.0; var sumXY = 0.0; var sumXX = 0.0
        for (i in 0 until n) {
            val x = i.toDouble()
            val y = samples[i].toDouble()
            sumX += x; sumY += y; sumXY += x * y; sumXX += x * x
        }
        val denom = n * sumXX - sumX * sumX
        val slope = if (abs(denom) < 1e-12) 0.0 else (n * sumXY - sumX * sumY) / denom
        val intercept = (sumY - slope * sumX) / n
        return FloatArray(n) { i -> (samples[i] - (slope * i + intercept)).toFloat() }
    }

    /** In-place Hann window. */
    fun applyHann(signal: FloatArray) {
        val n = signal.size
        if (n < 2) return
        for (i in 0 until n) {
            val w = 0.5 - 0.5 * cos(2.0 * Math.PI * i / (n - 1))
            signal[i] = (signal[i] * w).toFloat()
        }
    }
}
