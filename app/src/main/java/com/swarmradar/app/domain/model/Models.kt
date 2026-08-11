package com.swarmradar.app.domain.model

import org.ejml.simple.SimpleMatrix

/** Einfacher 3D-Vektor (frei von Android-/Sceneform-Abhängigkeiten). */
data class Vector3(val x: Double, val y: Double, val z: Double) {
    operator fun plus(o: Vector3) = Vector3(x + o.x, y + o.y, z + o.z)
    operator fun minus(o: Vector3) = Vector3(x - o.x, y - o.y, z - o.z)
    operator fun times(s: Double) = Vector3(x * s, y * s, z * s)
    operator fun div(s: Double) = Vector3(x / s, y / s, z / s)
}

/** Quaternion (x, y, z, w) für Orientierung. */
data class Quaternion(val x: Double, val y: Double, val z: Double, val w: Double)

enum class ObjectType { PERSON, ELECTRICAL, EXIT, REMOTE_DEVICE, UNKNOWN }

enum class ObjectSource { LOCAL, REMOTE, FUSED }

/** Ein einzelner 3D-Punkt der fusionierten Punktwolke. */
data class Point3D(
    val x: Float,
    val y: Float,
    val z: Float,
    val intensity: Float = 1.0f,
    val source: String = "local"
)

/** Erkanntes Objekt (Person, Elektro, Ausgang, Remote-Client). */
data class DetectedObject(
    val id: String,
    val type: ObjectType,
    val position: Vector3,
    val confidence: Float,
    val source: ObjectSource = ObjectSource.LOCAL,
    val breathingRate: Float? = null,
    val heartRate: Float? = null
)

data class Person(
    val id: String,
    val x: Float,
    val y: Float,
    val z: Float,
    val confidence: Float,
    val breathingRate: Float
)

/** Zustand des (erweiterten) Kalman-Filters. */
data class EKFState(
    val position: Vector3,
    val velocity: Vector3,
    val orientation: Quaternion,
    val covariance: DoubleArray
) {
    override fun equals(other: Any?): Boolean {
        if (this === other) return true
        if (javaClass != other?.javaClass) return false
        other as EKFState
        return position == other.position &&
            velocity == other.velocity &&
            orientation == other.orientation &&
            covariance.contentEquals(other.covariance)
    }

    override fun hashCode(): Int {
        var result = position.hashCode()
        result = 31 * result + velocity.hashCode()
        result = 31 * result + orientation.hashCode()
        result = 31 * result + covariance.contentHashCode()
        return result
    }
}

/** Global fusionierter Zustand (vom CollaborativeEKF ausgegeben). */
data class FusedState(
    val position: Vector3,
    val orientation: Quaternion,
    val covariance: SimpleMatrix
)

/** Starrkörper-Transformation eines Client-Koordinatensystems. */
data class RigidTransform(
    val rotation: SimpleMatrix, // 3x3
    val translation: Vector3
)

data class Session(
    val id: Long,
    val startTime: Long,
    val endTime: Long?,
    val pointCount: Int,
    val hash: String? = null
)

// ---- Sensor-Frames ----

data class ImuFrame(val accel: Vector3, val gyro: Vector3, val timestamp: Long)

data class RadarTarget(
    val x: Float,
    val y: Float,
    val z: Float,
    val velocity: Float
)

data class RadarFrame(val timestamp: Long, val targets: List<RadarTarget>)

data class BleMeasurement(
    val deviceAddress: String,
    val rssi: Int,
    val distance: Double,
    val timestamp: Long
)

enum class SwarmRole { MASTER, SLAVE, PEER }
