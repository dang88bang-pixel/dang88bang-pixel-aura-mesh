package com.aura.agent.fusion

import java.io.Closeable
import kotlin.math.sqrt

/**
 * Kotlin facade over the native 15-state EKF (`aura_core.cpp`).
 *
 * The filter lives entirely in native memory; this class only holds an opaque
 * handle. That keeps the per-sample JNI cost to a single call with primitive
 * arguments instead of marshalling a 15x15 covariance matrix at 200 Hz.
 *
 * The instance is **not** thread-safe by itself: the native side guards the
 * handle table, but two threads calling [predict] concurrently would still
 * interleave filter state. Drive it from one sensor thread (see
 * [SensorFusionService]).
 *
 * State layout (indices into [state]):
 * ```
 *  0..2  position    x, y, z            [m]
 *  3..5  velocity    vx, vy, vz         [m/s]
 *  6..8  attitude    roll, pitch, yaw   [rad]
 *  9..11 gyro bias                      [rad/s]
 * 12..14 accel bias                     [m/s^2]
 * ```
 */
class NativeEkf : Closeable {

    private var handle: Long = nativeCreate()
    private var closed = false

    val isValid: Boolean get() = !closed && handle != 0L

    /** IMU propagation. [gyro] in rad/s, [accel] specific force in m/s^2. */
    fun predict(gyro: FloatArray, accel: FloatArray, dt: Float) {
        require(gyro.size >= 3 && accel.size >= 3) { "gyro and accel need 3 components" }
        if (isValid) nativePredict(handle, gyro, accel, dt)
    }

    /** Absolute position fix (e.g. from a surveyed marker or GNSS). */
    fun updatePosition(position: FloatArray, sigma: Float = 0.5f) {
        require(position.size >= 3) { "position needs 3 components" }
        if (isValid) nativeUpdatePosition(handle, position, sigma)
    }

    /** Two-way-ranging measurement against a known UWB anchor. */
    fun updateUwbRange(anchor: FloatArray, distance: Float, sigma: Float = 0.12f) {
        require(anchor.size >= 3) { "anchor needs 3 components" }
        if (isValid) nativeUpdateUwbRange(handle, anchor, distance, sigma)
    }

    /** Magnetometer / compass heading in rad. */
    fun updateYaw(yaw: Float, sigma: Float = 0.35f) {
        if (isValid) nativeUpdateYaw(handle, yaw, sigma)
    }

    /** Zero-velocity update. Only call this when the device is *really* still. */
    fun updateZeroVelocity(sigma: Float = 0.02f) {
        if (isValid) nativeUpdateZeroVelocity(handle, sigma)
    }

    /** Barometric or assumed-carry-height altitude prior. */
    fun updateAltitude(altitude: Float, sigma: Float = 0.45f) {
        if (isValid) nativeUpdateAltitude(handle, altitude, sigma)
    }

    /**
     * 2D LiDAR scan-match result.
     *
     * Note there is no `z` parameter: a planar scan match observes x, y and
     * yaw only. Passing the filter's own altitude back in would shrink the
     * vertical covariance without adding information and let z drift away.
     */
    fun updateLidarPose(x: Float, y: Float, yaw: Float,
                        sigmaXy: Float = 0.06f, sigmaYaw: Float = 0.10f) {
        if (isValid) nativeUpdateLidarPose(handle, x, y, yaw, sigmaXy, sigmaYaw)
    }

    fun reset() {
        if (isValid) nativeReset(handle)
    }

    /** Full 15-element state vector, or an empty array if the filter is closed. */
    fun state(): FloatArray = if (isValid) nativeGetState(handle) ?: FloatArray(15) else FloatArray(15)

    fun covarianceDiagonal(): FloatArray =
        if (isValid) nativeGetCovarianceDiagonal(handle) ?: FloatArray(15) else FloatArray(15)

    /** Orientation as (x, y, z, w) - the ordering Babylon.js and Three.js expect. */
    fun quaternion(): FloatArray =
        if (isValid) nativeGetQuaternion(handle) ?: floatArrayOf(0f, 0f, 0f, 1f)
        else floatArrayOf(0f, 0f, 0f, 1f)

    fun position(): FloatArray = state().copyOfRange(0, 3)

    fun velocity(): FloatArray = state().copyOfRange(3, 6)

    fun attitude(): FloatArray = state().copyOfRange(6, 9)

    /** Per-axis position standard deviation in metres. */
    fun positionSigma(): FloatArray {
        val diagonal = covarianceDiagonal()
        return floatArrayOf(
            sqrt(diagonal[0].coerceAtLeast(0f)),
            sqrt(diagonal[1].coerceAtLeast(0f)),
            sqrt(diagonal[2].coerceAtLeast(0f)),
        )
    }

    /** Heuristic used by the UI to show a "fix acquired" indicator. */
    fun isConverged(threshold: Float = 0.75f): Boolean =
        positionSigma().all { it < threshold }

    fun snapshot(timestamp: Long = System.currentTimeMillis()): EkfSnapshot {
        val s = state()
        return EkfSnapshot(
            timestamp = timestamp,
            position = s.copyOfRange(0, 3),
            velocity = s.copyOfRange(3, 6),
            attitude = s.copyOfRange(6, 9),
            gyroBias = s.copyOfRange(9, 12),
            accelBias = s.copyOfRange(12, 15),
            quaternion = quaternion(),
            positionSigma = positionSigma(),
            converged = isConverged(),
        )
    }

    override fun close() {
        if (!closed) {
            nativeDestroy(handle)
            handle = 0L
            closed = true
        }
    }

    // -- native bindings ------------------------------------------------
    private external fun nativeCreate(): Long
    private external fun nativeDestroy(handle: Long)
    private external fun nativeReset(handle: Long)
    private external fun nativePredict(handle: Long, gyro: FloatArray, accel: FloatArray, dt: Float)
    private external fun nativeUpdatePosition(handle: Long, position: FloatArray, sigma: Float)
    private external fun nativeUpdateUwbRange(handle: Long, anchor: FloatArray, distance: Float, sigma: Float)
    private external fun nativeUpdateYaw(handle: Long, yaw: Float, sigma: Float)
    private external fun nativeUpdateZeroVelocity(handle: Long, sigma: Float)
    private external fun nativeUpdateAltitude(handle: Long, altitude: Float, sigma: Float)
    private external fun nativeUpdateLidarPose(
        handle: Long, x: Float, y: Float, yaw: Float, sigmaXy: Float, sigmaYaw: Float
    )
    private external fun nativeGetState(handle: Long): FloatArray?
    private external fun nativeGetCovarianceDiagonal(handle: Long): FloatArray?
    private external fun nativeGetQuaternion(handle: Long): FloatArray?

    companion object {
        init {
            System.loadLibrary("aura_core")
        }
    }
}

/** Immutable view of the filter state, safe to hand to the UI thread. */
data class EkfSnapshot(
    val timestamp: Long,
    val position: FloatArray,
    val velocity: FloatArray,
    val attitude: FloatArray,
    val gyroBias: FloatArray,
    val accelBias: FloatArray,
    val quaternion: FloatArray,
    val positionSigma: FloatArray,
    val converged: Boolean,
) {
    val speed: Float
        get() = sqrt(velocity[0] * velocity[0] + velocity[1] * velocity[1] + velocity[2] * velocity[2])

    val yaw: Float get() = attitude[2]

    fun toJson(): String = buildString {
        append("{")
        append("\"timestamp\":").append(timestamp).append(',')
        append("\"position\":").append(position.joinToString(",", "[", "]")).append(',')
        append("\"velocity\":").append(velocity.joinToString(",", "[", "]")).append(',')
        append("\"attitude\":").append(attitude.joinToString(",", "[", "]")).append(',')
        append("\"quaternion\":").append(quaternion.joinToString(",", "[", "]")).append(',')
        append("\"position_sigma\":").append(positionSigma.joinToString(",", "[", "]")).append(',')
        append("\"converged\":").append(converged)
        append("}")
    }

    // data class with arrays needs explicit equals/hashCode
    override fun equals(other: Any?): Boolean {
        if (this === other) return true
        if (other !is EkfSnapshot) return false
        return timestamp == other.timestamp &&
            position.contentEquals(other.position) &&
            velocity.contentEquals(other.velocity) &&
            attitude.contentEquals(other.attitude) &&
            quaternion.contentEquals(other.quaternion)
    }

    override fun hashCode(): Int {
        var result = timestamp.hashCode()
        result = 31 * result + position.contentHashCode()
        result = 31 * result + velocity.contentHashCode()
        result = 31 * result + attitude.contentHashCode()
        result = 31 * result + quaternion.contentHashCode()
        return result
    }
}
