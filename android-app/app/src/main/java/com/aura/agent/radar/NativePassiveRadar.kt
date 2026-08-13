package com.aura.agent.radar

import java.nio.ByteBuffer
import java.nio.ByteOrder
import kotlin.math.abs
import kotlin.math.log10
import kotlin.math.max
import kotlin.math.pow

/**
 * Passive bistatic radar over an attached SDR (RTL-SDR / HackRF on USB-C).
 *
 * Two receive channels are required: a **reference** pointed at the
 * illuminator (DVB-T, DAB, FM) and a **surveillance** channel watching the
 * scene. Targets appear as peaks of the cross-ambiguity function once the
 * direct path — typically 60–90 dB stronger than any echo — has been projected
 * out by the ECA canceller.
 *
 * All heavy lifting is in `aura_core.cpp`; this class only marshals IQ data.
 *
 * ## Resolution is a hard physical limit
 *
 * Range resolution is `c / (2·B)`. No amount of processing improves it:
 *
 * | SDR / illuminator            | Bandwidth | Range resolution |
 * |------------------------------|-----------|------------------|
 * | RTL-SDR (RTL2832U)           | 2.4 MHz   | **62.5 m**       |
 * | HackRF One                   | 20 MHz    | 7.5 m            |
 * | Full DVB-T channel, wideband | 8 MHz     | 18.7 m           |
 *
 * Doppler resolution is `1 / T`, where `T` is the dwell. A 16 384-sample record
 * at 2.4 MSps lasts 6.8 ms, so Doppler cells are ~146 Hz wide — a 70 Hz target
 * simply cannot be separated from zero Doppler at that dwell, regardless of how
 * finely the CAF is evaluated. Use [requiredSamplesForDoppler] before
 * promising a velocity figure to the operator.
 *
 * See `docs/performance_targets.md#3`.
 */
class NativePassiveRadar(
    val sampleRate: Float = 2.4e6f,
    val carrierHz: Float = 626e6f,
) {

    /**
     * Reusable direct buffers.
     *
     * `GetDirectBufferAddress` on the native side is zero-copy only for direct
     * buffers; a `FloatArray` would be copied twice per frame, and at 32 768
     * complex samples that is 512 kB of churn per call.
     */
    private var surveillanceBuffer: ByteBuffer? = null
    private var referenceBuffer: ByteBuffer? = null
    private var capacity = 0

    private fun ensureCapacity(samples: Int) {
        if (capacity >= samples && surveillanceBuffer != null) return
        capacity = samples
        val bytes = samples * 2 * Float.SIZE_BYTES     // interleaved I/Q
        surveillanceBuffer = ByteBuffer.allocateDirect(bytes).order(ByteOrder.nativeOrder())
        referenceBuffer = ByteBuffer.allocateDirect(bytes).order(ByteOrder.nativeOrder())
    }

    /**
     * Process one dwell.
     *
     * @param surveillance interleaved I/Q, length `2 * sampleCount`
     * @param reference    interleaved I/Q, length `2 * sampleCount`
     * @param ecaTaps      clutter-canceller taps; 0 disables cancellation
     *                     (only sensible when there is no direct path at all)
     */
    fun process(
        surveillance: FloatArray,
        reference: FloatArray,
        maxRangeBins: Int = 48,
        numBatches: Int = 64,
        ecaTaps: Int = 16,
    ): RangeDopplerMap? {
        val sampleCount = minOf(surveillance.size, reference.size) / 2
        if (sampleCount < 8) return null
        ensureCapacity(sampleCount)

        val surv = surveillanceBuffer ?: return null
        val ref = referenceBuffer ?: return null
        surv.clear()
        ref.clear()
        surv.asFloatBuffer().put(surveillance, 0, sampleCount * 2)
        ref.asFloatBuffer().put(reference, 0, sampleCount * 2)

        val raw = nativeProcess(
            surv, ref, sampleCount, sampleRate, maxRangeBins, numBatches, ecaTaps,
        ) ?: return null
        if (raw.size < 3) return null

        // layout: [rangeBins, dopplerBins, integrationTime, ...magnitude]
        val rangeBins = raw[0].toInt()
        val dopplerBins = raw[1].toInt()
        val integrationTime = raw[2]
        if (rangeBins <= 0 || dopplerBins <= 0) return null
        val expected = rangeBins * dopplerBins
        if (raw.size < 3 + expected) return null

        return RangeDopplerMap(
            magnitude = raw.copyOfRange(3, 3 + expected),
            rangeBins = rangeBins,
            dopplerBins = dopplerBins,
            sampleRate = sampleRate,
            carrierHz = carrierHz,
            integrationTime = integrationTime,
        )
    }

    /** Cell-averaging CFAR over a range-Doppler map. */
    fun cfar(map: RangeDopplerMap, thresholdDb: Float = 12.0f): BooleanArray? {
        val mask = nativeCfar(map.magnitude, map.dopplerBins, map.rangeBins, thresholdDb)
            ?: return null
        return BooleanArray(mask.size) { mask[it].toInt() != 0 }
    }

    /**
     * CFAR plus peak picking.
     *
     * The zero-Doppler ridge is excluded: it is stationary clutter the ECA
     * canceller could not fully remove, never a moving target.
     */
    fun detect(
        map: RangeDopplerMap,
        thresholdDb: Float = 12.0f,
        maxTargets: Int = 10,
        excludeZeroDopplerHz: Float = 0.5f,
    ): List<RadarDetection> {
        val mask = cfar(map, thresholdDb) ?: return emptyList()
        val power = FloatArray(map.magnitude.size) { map.magnitude[it] * map.magnitude[it] }

        val positive = power.filter { it > 0f }.sorted()
        val noiseFloor = if (positive.isEmpty()) 1e-12f else positive[positive.size / 2]

        val detections = mutableListOf<RadarDetection>()
        for (d in 0 until map.dopplerBins) {
            val doppler = map.dopplerHz(d)
            if (abs(doppler) < excludeZeroDopplerHz) continue
            for (r in 0 until map.rangeBins) {
                val index = d * map.rangeBins + r
                if (!mask[index]) continue

                // 3x3 local maximum, so one target yields one detection
                var isPeak = true
                loop@ for (dd in -1..1) {
                    for (dr in -1..1) {
                        val nd = d + dd
                        val nr = r + dr
                        if (nd !in 0 until map.dopplerBins || nr !in 0 until map.rangeBins) continue
                        if (power[nd * map.rangeBins + nr] > power[index]) {
                            isPeak = false
                            break@loop
                        }
                    }
                }
                if (!isPeak) continue

                detections += RadarDetection(
                    bistaticRange = map.rangeMetres(r),
                    dopplerHz = doppler,
                    velocity = map.velocity(d),
                    snrDb = (10.0 * log10(max(power[index], 1e-30f).toDouble() /
                        max(noiseFloor, 1e-30f).toDouble())).toFloat(),
                    rangeBin = r,
                    dopplerBin = d,
                )
            }
        }
        return detections.sortedByDescending { it.snrDb }.take(maxTargets)
    }

    private external fun nativeProcess(
        surveillance: ByteBuffer,
        reference: ByteBuffer,
        sampleCount: Int,
        sampleRate: Float,
        maxRangeBins: Int,
        numBatches: Int,
        ecaTaps: Int,
    ): FloatArray?

    private external fun nativeCfar(
        magnitude: FloatArray, rows: Int, cols: Int, thresholdDb: Float,
    ): ByteArray?

    companion object {
        const val SPEED_OF_LIGHT = 299_792_458.0f

        init {
            System.loadLibrary("aura_core")
        }

        /** `c / (2B)` — the physical limit. */
        fun rangeResolution(bandwidthHz: Float): Float =
            SPEED_OF_LIGHT / (2.0f * max(bandwidthHz, 1.0f))

        /** `lambda / (2T)` — a longer dwell buys finer velocity resolution. */
        fun velocityResolution(carrierHz: Float, integrationTime: Float): Float =
            (SPEED_OF_LIGHT / max(carrierHz, 1.0f)) / (2.0f * max(integrationTime, 1e-9f))

        /** `1 / T` — the smallest resolvable Doppler shift. */
        fun dopplerResolution(integrationTime: Float): Float =
            1.0f / max(integrationTime, 1e-9f)

        /**
         * How many samples a dwell needs to resolve [targetHz].
         *
         * Call this before quoting a velocity: asking the CAF for a 70 Hz bin
         * on a 6.8 ms dwell returns a number, but it is interpolation inside a
         * single 146 Hz resolution cell, not resolving power.
         */
        fun requiredSamplesForDoppler(targetHz: Float, sampleRate: Float): Int =
            if (targetHz <= 0f) 0 else kotlin.math.ceil(sampleRate / targetHz).toInt()
    }
}

/** CAF magnitude over (bistatic range, Doppler). */
data class RangeDopplerMap(
    val magnitude: FloatArray,        // dopplerBins * rangeBins, row-major
    val rangeBins: Int,
    val dopplerBins: Int,
    val sampleRate: Float,
    val carrierHz: Float,
    val integrationTime: Float,
) {
    /** Doppler axis is fftshifted, so bin `dopplerBins/2` is zero Doppler. */
    fun dopplerHz(bin: Int): Float {
        val batchRate = sampleRate / (sampleRate * integrationTime / dopplerBins)
        val binWidth = batchRate / dopplerBins
        return (bin - dopplerBins / 2) * binWidth
    }

    fun rangeMetres(bin: Int): Float =
        bin * NativePassiveRadar.SPEED_OF_LIGHT / sampleRate

    fun velocity(dopplerBin: Int): Float =
        dopplerHz(dopplerBin) * (NativePassiveRadar.SPEED_OF_LIGHT / carrierHz) / 2.0f

    fun at(dopplerBin: Int, rangeBin: Int): Float = magnitude[dopplerBin * rangeBins + rangeBin]

    /** Normalised dB, floored, for display. */
    fun toDb(floorDb: Float = -80.0f): FloatArray {
        val peak = magnitude.maxOrNull() ?: 0f
        if (peak <= 0f) return FloatArray(magnitude.size) { floorDb }
        return FloatArray(magnitude.size) {
            max(floorDb, (20.0 * log10(max(magnitude[it] / peak, 1e-12f).toDouble())).toFloat())
        }
    }

    override fun equals(other: Any?): Boolean =
        other is RangeDopplerMap && rangeBins == other.rangeBins &&
            dopplerBins == other.dopplerBins && magnitude.contentEquals(other.magnitude)

    override fun hashCode(): Int = 31 * (31 * rangeBins + dopplerBins) + magnitude.contentHashCode()
}

/** A CAF peak that survived CFAR. */
data class RadarDetection(
    val bistaticRange: Float,
    val dopplerHz: Float,
    val velocity: Float,
    val snrDb: Float,
    val rangeBin: Int,
    val dopplerBin: Int,
)
