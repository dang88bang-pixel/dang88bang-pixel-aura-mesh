package com.swarmradar.app.domain.repository

import com.swarmradar.app.domain.model.BleMeasurement
import com.swarmradar.app.domain.model.ImuFrame
import com.swarmradar.app.domain.model.Person
import com.swarmradar.app.domain.model.Point3D
import com.swarmradar.app.domain.model.RadarFrame
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow

interface ISensorRepository {
    val radarData: Flow<RadarFrame>
    val imuData: Flow<ImuFrame>
    val bleMeasurements: Flow<BleMeasurement>
    fun start()
    fun stop()
}

interface IPointCloudRepository {
    fun addPoints(points: List<Point3D>, sourceId: String)
    fun getDownsampledPoints(): List<Point3D>
}

interface IPersonRepository {
    val persons: MutableStateFlow<List<Person>>
    fun reportPerson(p: Person)
}
