package com.swarmradar.app.data.local.entities

import androidx.room.Entity
import androidx.room.PrimaryKey
import androidx.room.TypeConverter
import androidx.room.TypeConverters
import com.swarmradar.app.domain.model.Point3D

@Entity(tableName = "sessions")
data class SessionEntity(
    @PrimaryKey(autoGenerate = true) val id: Long = 0,
    val startTime: Long,
    val endTime: Long?,
    val pointCount: Int,
    val hash: String? = null
)

@Entity(tableName = "trajectory")
data class TrajectoryPointEntity(
    @PrimaryKey(autoGenerate = true) val id: Long = 0,
    val sessionId: Long,
    val timestamp: Long,
    val x: Double, val y: Double, val z: Double,
    val vx: Double, val vy: Double, val vz: Double
)

@Entity(tableName = "persons")
data class PersonEntity(
    @PrimaryKey(autoGenerate = true) val id: Long = 0,
    val sessionId: Long,
    val timestamp: Long,
    val x: Float, val y: Float, val z: Float,
    val confidence: Float,
    val breathingRate: Float
)

@Entity(tableName = "pointcloud_chunks")
data class PointCloudChunkEntity(
    @PrimaryKey(autoGenerate = true) val id: Long = 0,
    val sessionId: Long,
    val sourceId: String,
    val points: String // serialisiert: "x,y,z,intensity;x,y,z,intensity;..."
)

class Converters {
    @TypeConverter
    fun fromPoint3DList(list: List<Point3D>): String =
        list.joinToString(";") { "${it.x},${it.y},${it.z},${it.intensity}" }

    @TypeConverter
    fun toPoint3DList(s: String): List<Point3D> =
        if (s.isBlank()) emptyList() else s.split(";").map { p ->
            val (x, y, z, i) = p.split(",")
            Point3D(x.toFloat(), y.toFloat(), z.toFloat(), i.toFloat())
        }
}
