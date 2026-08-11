package com.swarmradar.app.domain.ekf

import com.swarmradar.app.domain.alignment.UmeyamaAlignment
import com.swarmradar.app.domain.model.FusedState
import com.swarmradar.app.domain.model.ImuFrame
import com.swarmradar.app.domain.model.Quaternion
import com.swarmradar.app.domain.model.RadarFrame
import com.swarmradar.app.domain.model.RadarTarget
import com.swarmradar.app.domain.model.RigidTransform
import com.swarmradar.app.domain.model.Vector3
import org.ejml.simple.SimpleMatrix
import javax.inject.Inject
import javax.inject.Singleton

/**
 * Kollaborativer EKF (Zustand: [x,y,z,vx,vy,vz,qw,qx,qy,qz]).
 * Führt die Sensordaten mehrerer Clients in einem globalen Koordinatensystem zusammen.
 */
@Singleton
class CollaborativeEKF @Inject constructor(
    private val alignmentCalculator: UmeyamaAlignment
) {
    // Zustand: [x, y, z, vx, vy, vz, qw, qx, qy, qz]
    private val state = SimpleMatrix(10, 1)
    private val covariance = SimpleMatrix(10, 10).identity()
    private val clientTransforms = mutableMapOf<String, RigidTransform>()

    fun predict(imu: ImuFrame, dt: Double) {
        // Constant-Velocity-Prädiktion für Position/Geschwindigkeit
        state[0] += state[3] * dt + 0.5 * imu.accel.x * dt * dt
        state[1] += state[4] * dt + 0.5 * imu.accel.y * dt * dt
        state[2] += state[5] * dt + 0.5 * imu.accel.z * dt * dt
        state[3] += imu.accel.x * dt
        state[4] += imu.accel.y * dt
        state[5] += imu.accel.z * dt
        covariance[0, 0] += 0.01; covariance[1, 1] += 0.01; covariance[2, 2] += 0.01
    }

    fun updateWithRadar(radar: RadarFrame, deviceId: String) {
        val transform = clientTransforms[deviceId] ?: return
        val globalTargets = radar.targets.map { transform.applyTo(it) }
        // Hier würde der EKF-Update-Schritt mit den transformierten Zielen erfolgen.
    }

    fun registerClient(deviceId: String, transform: RigidTransform) {
        clientTransforms[deviceId] = transform
    }

    fun getFusedState(): FusedState = FusedState(
        position = Vector3(state[0], state[1], state[2]),
        orientation = Quaternion(state[6], state[7], state[8], state[9]),
        covariance = covariance
    )

    private fun RigidTransform.applyTo(t: RadarTarget): RadarTarget {
        val p = SimpleMatrix(3, 1).apply { set(0, 0, t.x.toDouble()); set(1, 0, t.y.toDouble()); set(2, 0, t.z.toDouble()) }
        val r = rotation.mult(p)
        return RadarTarget(r[0].toFloat(), r[1].toFloat(), r[2].toFloat(), t.velocity)
    }
}
