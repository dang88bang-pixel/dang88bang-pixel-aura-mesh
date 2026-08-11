package com.swarmradar.app.domain.mapping

import com.swarmradar.app.domain.model.Point3D
import javax.inject.Inject
import javax.inject.Singleton

/**
 * Voxel-basierter Downsampler: reduziert Redundanz in fusionierten Punktwolken
 * (5 cm Voxel-Größe), bevor sie persistiert/gerendert werden.
 */
@Singleton
class PointCloudFuser @Inject constructor() {
    private val voxelSize = 0.05 // 5 cm
    private val voxelGrid = mutableMapOf<VoxelKey, MutableList<Point3D>>()

    fun addPoints(points: List<Point3D>, sourceId: String) {
        points.forEach { point ->
            val key = VoxelKey(
                (point.x / voxelSize).toInt(),
                (point.y / voxelSize).toInt(),
                (point.z / voxelSize).toInt()
            )
            voxelGrid.getOrPut(key) { mutableListOf() }.add(point)
        }
    }

    fun getDownsampledPoints(): List<Point3D> =
        voxelGrid.values.map { pts ->
            val avgX = pts.map { it.x }.average().toFloat()
            val avgY = pts.map { it.y }.average().toFloat()
            val avgZ = pts.map { it.z }.average().toFloat()
            Point3D(avgX, avgY, avgZ, 1.0f)
        }

    private data class VoxelKey(val ix: Int, val iy: Int, val iz: Int)
}
