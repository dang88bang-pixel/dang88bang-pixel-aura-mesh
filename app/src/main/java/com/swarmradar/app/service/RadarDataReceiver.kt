package com.swarmradar.app.service

import com.swarmradar.app.domain.model.RadarFrame
import com.swarmradar.app.domain.model.Vector3
import javax.inject.Inject
import javax.inject.Singleton

/**
 * Aggregiert eingehende Radar-Frames (z. B. von UDP oder MQTT) in einen
 * gepufferten Strom für den CollaborativeEKF.
 */
@Singleton
class RadarDataReceiver @Inject constructor() {
    // In Produktion: hier würde das Frame in den Fusions-Puffer gelegt.
    fun onRadarFrame(frame: RadarFrame): List<Vector3> =
        frame.targets.map { Vector3(it.x.toDouble(), it.y.toDouble(), it.z.toDouble()) }
}
