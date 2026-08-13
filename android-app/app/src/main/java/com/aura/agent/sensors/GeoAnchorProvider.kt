package com.aura.agent.sensors

import android.Manifest
import android.annotation.SuppressLint
import android.content.Context
import android.content.pm.PackageManager
import android.location.Location
import android.location.LocationListener
import android.location.LocationManager
import android.os.Build
import android.os.Bundle
import android.util.Log
import androidx.core.content.ContextCompat
import kotlin.math.abs

private const val TAG = "GeoAnchor"

/**
 * Acquires the WGS84 anchor that ties AURA's local metric frame to the Earth.
 *
 * Why this exists
 * ---------------
 * The EKF works in metres from wherever the agent started. Everything
 * geo-referenced -- CoT export, the mesh frame, any map overlay -- needs to
 * know where that origin sits on the planet. Nothing in the fusion pipeline
 * can derive it; it has to be measured or entered.
 *
 * The intended field flow is therefore: stand outside where the sky is open,
 * take a fix, then walk in. Indoors GNSS is either unavailable or, worse,
 * confidently wrong from multipath.
 *
 * Accuracy is the whole point
 * ---------------------------
 * The anchor's error is added in quadrature to every position AURA publishes,
 * and it normally **dominates**: the EKF may be good to 0.06 m relative to the
 * origin, but a 5 m handheld fix makes every absolute position a 5 m position.
 * So this class never reports a fix without its accuracy, and refuses fixes
 * worse than [maxAcceptableAccuracyM] rather than quietly anchoring the whole
 * operation to a bad one.
 *
 * `Location.getAccuracy()` is documented as 68% (1-sigma) horizontal radius,
 * which is exactly what the agent's `sigma_m` expects -- no conversion.
 */
class GeoAnchorProvider(
    private val context: Context,
    /** Reject fixes worse than this. 25 m is already poor for an anchor. */
    private val maxAcceptableAccuracyM: Float = 25.0f,
) {

    data class Fix(
        val latitude: Double,
        val longitude: Double,
        /** Height above the WGS84 *ellipsoid*, metres. See [hae]. */
        val hae: Double,
        /** 1-sigma horizontal accuracy, metres, straight from the provider. */
        val sigmaM: Double,
        val provider: String,
        val ageMs: Long,
    )

    private val locationManager: LocationManager? =
        ContextCompat.getSystemService(context, LocationManager::class.java)

    private var listener: LocationListener? = null

    fun hasPermission(): Boolean =
        ContextCompat.checkSelfPermission(context, Manifest.permission.ACCESS_FINE_LOCATION) ==
            PackageManager.PERMISSION_GRANTED

    fun isGnssEnabled(): Boolean =
        locationManager?.isProviderEnabled(LocationManager.GPS_PROVIDER) == true

    /**
     * Request a single fresh fix.
     *
     * Deliberately not `getLastKnownLocation`: a cached fix from a different
     * building is the exact failure this class exists to prevent, and it comes
     * back instantly and looks fine.
     *
     * @param onResult receives the fix, or null with a reason for the operator.
     */
    @SuppressLint("MissingPermission")   // guarded by hasPermission() below
    fun requestFix(onResult: (Fix?, String) -> Unit) {
        val manager = locationManager
        if (manager == null) {
            onResult(null, "no location service on this device")
            return
        }
        if (!hasPermission()) {
            onResult(null, "ACCESS_FINE_LOCATION not granted")
            return
        }
        if (!isGnssEnabled()) {
            onResult(null, "GNSS is switched off")
            return
        }

        stop()
        val handler = object : LocationListener {
            override fun onLocationChanged(location: Location) {
                stop()
                onResult(evaluate(location), describe(location))
            }

            // Required on API < 30; removing them breaks older runtimes.
            @Deprecated("required for API < 30")
            override fun onStatusChanged(provider: String?, status: Int, extras: Bundle?) = Unit
            override fun onProviderEnabled(provider: String) = Unit
            override fun onProviderDisabled(provider: String) {
                stop()
                onResult(null, "GNSS disabled while waiting for a fix")
            }
        }
        listener = handler

        runCatching {
            manager.requestLocationUpdates(
                LocationManager.GPS_PROVIDER,
                0L,
                0f,
                handler,
                context.mainLooper,
            )
        }.onFailure {
            stop()
            onResult(null, "could not start GNSS: ${it.message}")
        }
    }

    /** Stop listening. Safe to call when not started. */
    fun stop() {
        listener?.let { runCatching { locationManager?.removeUpdates(it) } }
        listener = null
    }

    /**
     * Turn a raw [Location] into a [Fix], or null if it is not good enough
     * to anchor an operation to.
     */
    fun evaluate(location: Location): Fix? {
        if (!location.hasAccuracy()) {
            // A fix without a stated accuracy cannot be reported honestly, and
            // guessing one would defeat the purpose of carrying sigma at all.
            Log.w(TAG, "fix has no accuracy; rejected")
            return null
        }
        val accuracy = location.accuracy
        if (!accuracy.isFinite() || accuracy <= 0f || accuracy > maxAcceptableAccuracyM) {
            Log.w(TAG, "fix accuracy ${accuracy}m outside 0..$maxAcceptableAccuracyM; rejected")
            return null
        }
        if (abs(location.latitude) > 90.0 || abs(location.longitude) > 180.0) {
            Log.w(TAG, "fix out of range; rejected")
            return null
        }

        val ageMs = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            (android.os.SystemClock.elapsedRealtimeNanos() - location.elapsedRealtimeNanos) / 1_000_000
        } else {
            System.currentTimeMillis() - location.time
        }

        return Fix(
            latitude = location.latitude,
            longitude = location.longitude,
            // Android reports altitude above the WGS84 ellipsoid, which is what
            // CoT's `hae` wants. Do not "correct" this to MSL -- conflating the
            // two is the classic source of tens of metres of vertical error.
            hae = if (location.hasAltitude()) location.altitude else 0.0,
            sigmaM = accuracy.toDouble(),
            provider = location.provider ?: LocationManager.GPS_PROVIDER,
            ageMs = ageMs.coerceAtLeast(0L),
        )
    }

    private fun describe(location: Location): String =
        if (location.hasAccuracy()) {
            "fix from ${location.provider}, +/-%.1f m".format(location.accuracy)
        } else {
            "fix from ${location.provider}, accuracy unknown"
        }

    companion object {
        /**
         * Bearing of the local +x axis, degrees clockwise from **true** north.
         *
         * A magnetometer reads *magnetic* north. Feeding that straight in tilts
         * the whole map by the local declination -- about 4 degrees E in
         * central Europe, which is ~7 m of cross-track error at 100 m. The
         * caller must apply `GeomagneticField.getDeclination()` first, and this
         * helper exists so the conversion is written down once.
         */
        fun trueBearing(magneticBearingDeg: Float, declinationDeg: Float): Double {
            var bearing = (magneticBearingDeg + declinationDeg).toDouble() % 360.0
            if (bearing < 0) bearing += 360.0
            return bearing
        }
    }
}
