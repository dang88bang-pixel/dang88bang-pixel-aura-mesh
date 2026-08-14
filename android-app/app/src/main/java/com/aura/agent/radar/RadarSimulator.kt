package com.aura.agent.radar

import kotlin.math.roundToInt

/**
 * Physics-based synthetic IQ for the passive-radar feed.
 *
 * Generates the two channels a bistatic radar needs:
 *
 *  * **reference** — the illuminator signal as captured by an antenna pointed
 *    at the source. Complex white noise, which is exactly what a DVB-T/DAB
 *    channel looks like at baseband: wideband and flat, so the CAF has a
 *    thumbtack ambiguity function.
 *  * **surveillance** — the same signal at `directPathGain` (the dominant
 *    clutter the ECA canceller must remove) plus, per target, a delayed and
 *    Doppler-rotated copy, plus receiver noise.
 *
 * This mirrors `edge-agent/aura/passive_radar.py::simulate_surveillance`, so
 * the same scene can be injected into the Python reference and the device
 * path. The convention that matters: the CAF maps lag *k* to range
 * `k · c / fs` (native and Python agree), so the echo is delayed by
 * `round(range · fs / c)` samples and the peak lands on the bin the operator
 * will read.
 *
 * Pure Kotlin — the delay/Doppler maths is host-tested in
 * `RadarSimulatorTest.kt`; the native CAF that consumes the output is
 * covered by `test_aura_core.cpp`.
 */
class RadarSimulator(
    val sampleRate: Float = 2.4e6f,
    val carrierHz: Float = 626e6f,
    seed: Long = 0xA9E5C0DEL,
) {

    /** One target echo: bistatic range, Doppler shift, relative amplitude. */
    data class TargetSpec(
        val bistaticRangeM: Float,
        val dopplerHz: Float,
        val amplitude: Float = 0.01f,
    )

    /** A captured dwell: interleaved I/Q, `2 * sampleCount` floats each. */
    data class Dwell(
        val surveillance: FloatArray,
        val reference: FloatArray,
        val sampleCount: Int,
    )

    private val rng = java.util.Random(seed)

    /**
     * Synthesize one dwell of [durationMs].
     *
     * @param target        echo to inject, or null for a clutter-only scene
     * @param directPathGain strength of the direct path in the surveillance
     *                       channel (unitless, reference-normalised)
     * @param noiseSigma    receiver noise standard deviation
     */
    fun dwell(
        durationMs: Float = 6.8f,
        target: TargetSpec? = null,
        directPathGain: Float = 1.0f,
        noiseSigma: Float = 0.02f,
    ): Dwell {
        val n = (sampleRate * durationMs / 1000f).roundToInt().coerceAtLeast(256)

        // Reference: complex white noise, unit variance.
        val ref = FloatArray(2 * n)
        for (i in 0 until n) {
            ref[2 * i] = gaussian()
            ref[2 * i + 1] = gaussian()
        }

        val surv = FloatArray(2 * n)
        // Direct path — the clutter the ECA canceller will project out.
        for (i in 0 until n) {
            surv[2 * i] = directPathGain * ref[2 * i]
            surv[2 * i + 1] = directPathGain * ref[2 * i + 1]
        }

        target?.let { t ->
            val delay = delaySamples(t.bistaticRangeM)
            val omega = 2.0 * Math.PI * t.dopplerHz / sampleRate
            for (i in delay until n) {
                val phase = omega * i
                val c = Math.cos(phase).toFloat()
                val s = Math.sin(phase).toFloat()
                // (ei + j·eq) · (c + j·s): delayed reference, Doppler-rotated.
                val ei = ref[2 * (i - delay)]
                val eq = ref[2 * (i - delay) + 1]
                surv[2 * i] += t.amplitude * (ei * c - eq * s)
                surv[2 * i + 1] += t.amplitude * (ei * s + eq * c)
            }
        }

        for (i in 0 until n) {
            surv[2 * i] += noiseSigma * gaussian()
            surv[2 * i + 1] += noiseSigma * gaussian()
        }
        return Dwell(surv, ref, n)
    }

    /** CAF lag (in samples) for a target at [bistaticRangeM]. */
    fun delaySamples(bistaticRangeM: Float): Int =
        (bistaticRangeM * sampleRate / SPEED_OF_LIGHT).roundToInt()

    /** Inverse of [delaySamples] — what bin [lag] reads as in metres. */
    fun rangeMetres(lag: Int): Float =
        lag * SPEED_OF_LIGHT / sampleRate

    private fun gaussian(): Float {
        // Marsaglia polar method; deterministic per seed so CI is stable.
        var u1: Double
        var u2: Double
        var s: Double
        do {
            u1 = 2.0 * rng.nextDouble() - 1.0
            u2 = 2.0 * rng.nextDouble() - 1.0
            s = u1 * u1 + u2 * u2
        } while (s >= 1.0 || s == 0.0)
        val mag = Math.sqrt(-2.0 * Math.log(s) / s)
        return (u1 * mag).toFloat()
    }

    companion object {
        const val SPEED_OF_LIGHT = 299_792_458.0f
    }
}
