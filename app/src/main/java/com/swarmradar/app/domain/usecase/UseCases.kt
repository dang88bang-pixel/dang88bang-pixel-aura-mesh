package com.swarmradar.app.domain.usecase

import com.swarmradar.app.domain.alignment.UmeyamaAlignment
import com.swarmradar.app.domain.ekf.CollaborativeEKF
import com.swarmradar.app.domain.model.DetectedObject
import com.swarmradar.app.domain.model.EKFState
import com.swarmradar.app.domain.model.ImuFrame
import com.swarmradar.app.domain.model.Point3D
import com.swarmradar.app.domain.model.RadarFrame
import com.swarmradar.app.domain.model.RigidTransform
import com.swarmradar.app.domain.model.Vector3
import com.swarmradar.app.domain.repository.IPointCloudRepository
import com.swarmradar.app.domain.repository.IPersonRepository
import javax.inject.Inject

class StartScanUseCase @Inject constructor(
    private val sensorRepo: com.swarmradar.app.domain.repository.ISensorRepository
) {
    operator fun invoke() = sensorRepo.start()
}

class StopScanUseCase @Inject constructor(
    private val sensorRepo: com.swarmradar.app.domain.repository.ISensorRepository
) {
    operator fun invoke() = sensorRepo.stop()
}

class ProcessEKFUseCase @Inject constructor(
    private val ekf: CollaborativeEKF
) {
    operator fun invoke(imu: ImuFrame, dt: Double) = ekf.predict(imu, dt)
    fun update(radar: RadarFrame, deviceId: String) = ekf.updateWithRadar(radar, deviceId)
    fun register(deviceId: String, transform: RigidTransform) = ekf.registerClient(deviceId, transform)
    fun state(): EKFState = run {
        val s = ekf.getFusedState()
        EKFState(s.position, Vector3(0.0, 0.0, 0.0), s.orientation, DoubleArray(100))
    }
}

class FuseSwarmDataUseCase @Inject constructor(
    private val alignmentCalculator: UmeyamaAlignment,
    private val pointCloudRepo: IPointCloudRepository,
    private val personRepo: IPersonRepository
) {
    operator fun invoke(
        localPoints: List<Point3D>,
        remotePoints: List<Point3D>,
        detected: List<DetectedObject>
    ): List<Point3D> {
        // 1. Punktwolken transformiert zusammenführen (Voxel-Filter im Repo)
        pointCloudRepo.addPoints(localPoints, "local")
        pointCloudRepo.addPoints(remotePoints, "remote")
        // 2. Objekte übernehmen
        detected.filter { it.type == com.swarmradar.app.domain.model.ObjectType.PERSON }
            .forEach {
                personRepo.reportPerson(
                    com.swarmradar.app.domain.model.Person(
                        it.id, it.position.x.toFloat(), it.position.y.toFloat(), it.position.z.toFloat(),
                        it.confidence, it.breathingRate ?: 0f
                    )
                )
            }
        return pointCloudRepo.getDownsampledPoints()
    }
}

class DetectPersonsUseCase @Inject constructor(
    private val personRepo: IPersonRepository
) {
    operator fun invoke(): List<com.swarmradar.app.domain.model.Person> = personRepo.persons.value
}

class ExportMapUseCase @Inject constructor(
    private val pointCloudRepo: IPointCloudRepository
) {
    operator fun invoke(): String {
        // Vereinfachter CSV-Export der gedownsampleten Punktwolke.
        return pointCloudRepo.getDownsampledPoints().joinToString("\n") { "%.3f,%.3f,%.3f".format(it.x, it.y, it.z) }
    }
}
