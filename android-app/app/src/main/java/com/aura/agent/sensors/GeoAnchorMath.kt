package com.aura.agent.sensors

/**
 * The arithmetic behind the geo anchor, with **no Android imports**.
 *
 * Split out from `GeoAnchorProvider` on purpose: that class needs
 * `LocationManager`, so it can only run on a device or an emulator. The parts
 * that are easy to get quietly wrong -- declination, accuracy gating, error
 * combination -- are pure functions and live here, where
 * `tools/run-kotlin-tests.sh` compiles and runs them on the host JVM.
 *
 * See `docs/geo_anchor.md`.
 */
object GeoAnchorMath {

    /** Reject anchor fixes worse than this (metres, 1-sigma). */
    const val MAX_ANCHOR_SIGMA_M = 25.0

    /** A fix older than this is stale enough to be from somewhere else. */
    const val MAX_FIX_AGE_MS = 30_000L

    /**
     * Magnetic bearing -> **true** bearing, normalised to [0, 360).
     *
     * A magnetometer reads magnetic north. Using it directly rotates the whole
     * map by the local declination: about +4 deg in central Europe, which is
     * ~7 m of cross-track error at 100 m and grows with distance. Declination
     * comes from `android.hardware.GeomagneticField.getDeclination()`.
     */
    fun trueBearing(magneticBearingDeg: Double, declinationDeg: Double): Double {
        if (!magneticBearingDeg.isFinite() || !declinationDeg.isFinite()) return 0.0
        var bearing = (magneticBearingDeg + declinationDeg) % 360.0
        if (bearing < 0.0) bearing += 360.0
        return bearing
    }

    /**
     * Is this fix good enough to anchor an entire operation to?
     *
     * Rejecting is the safe outcome. Anchoring to a bad fix silently shifts
     * every position AURA later publishes, and nothing downstream can detect
     * it -- the markers look perfectly confident, just in the wrong place.
     */
    fun isAcceptableFix(
        sigmaM: Double,
        ageMs: Long,
        latitude: Double,
        longitude: Double,
    ): Boolean {
        if (!sigmaM.isFinite() || sigmaM <= 0.0 || sigmaM > MAX_ANCHOR_SIGMA_M) return false
        if (ageMs < 0L || ageMs > MAX_FIX_AGE_MS) return false
        if (!latitude.isFinite() || !longitude.isFinite()) return false
        if (latitude < -90.0 || latitude > 90.0) return false
        if (longitude < -180.0 || longitude > 180.0) return false
        return true
    }

    /**
     * Total horizontal 1-sigma of an exported position.
     *
     * The EKF error is relative to the origin; the anchor error is the origin's
     * own. They are independent, so they add in quadrature. Mirrors
     * `_combine_sigma` in `edge-agent/aura/cot.py` -- if one changes, change
     * both, or the handheld and the agent will disagree about accuracy.
     */
    fun combinedSigma(ekfSigmaM: Double, anchorSigmaM: Double): Double {
        val ekf = if (ekfSigmaM.isFinite() && ekfSigmaM > 0.0) ekfSigmaM else 0.0
        val anchor = if (anchorSigmaM.isFinite() && anchorSigmaM > 0.0) anchorSigmaM else 0.0
        return kotlin.math.sqrt(ekf * ekf + anchor * anchor)
    }

    /**
     * 1-sigma -> CoT `ce` (~95% circular error) via the 2D Rayleigh factor.
     * Must match `_sigma_to_ce` in `aura/cot.py`.
     */
    fun sigmaToCe(sigmaM: Double): Double = sigmaM * 2.4477

    /**
     * Which term dominates? Used by the UI to tell the operator whether a
     * better anchor would actually help, or whether the fusion is the limit.
     */
    fun anchorDominates(ekfSigmaM: Double, anchorSigmaM: Double): Boolean =
        anchorSigmaM > ekfSigmaM
}
