package com.swarmradar.app.data.repository

import com.swarmradar.app.data.sensor.SensorHardwareManager
import com.swarmradar.app.domain.mapping.PointCloudFuser
import com.swarmradar.app.domain.model.BleMeasurement
import com.swarmradar.app.domain.model.ImuFrame
import com.swarmradar.app.domain.model.Person
import com.swarmradar.app.domain.model.Point3D
import com.swarmradar.app.domain.model.RadarFrame
import com.swarmradar.app.domain.repository.IPersonRepository
import com.swarmradar.app.domain.repository.IPointCloudRepository
import com.swarmradar.app.domain.repository.ISensorRepository
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import javax.inject.Inject
import javax.inject.Singleton

@Singleton
class SensorRepositoryImpl @Inject constructor(
    private val hw: SensorHardwareManager
) : ISensorRepository {
    override val radarData: Flow<RadarFrame> = hw.radarData
    override val imuData: Flow<ImuFrame> = hw.imuData
    override val bleMeasurements: Flow<BleMeasurement> = hw.bleMeasurements
    override fun start() { hw.startUdpServer(); hw.registerImu() }
    override fun stop() = hw.stop()
}

@Singleton
class PointCloudRepositoryImpl @Inject constructor(
    private val fuser: PointCloudFuser
) : IPointCloudRepository {
    override fun addPoints(points: List<Point3D>, sourceId: String) =
        fuser.addPoints(points, sourceId)

    override fun getDownsampledPoints(): List<Point3D> =
        fuser.getDownsampledPoints()
}

@Singleton
class PersonRepositoryImpl @Inject constructor() : IPersonRepository {
    override val persons = MutableStateFlow<List<Person>>(emptyList())
    override fun reportPerson(p: Person) {
        persons.value = persons.value + p
    }
}
