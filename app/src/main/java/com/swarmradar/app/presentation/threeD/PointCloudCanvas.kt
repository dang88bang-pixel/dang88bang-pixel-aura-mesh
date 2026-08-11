package com.swarmradar.app.presentation.threeD

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import com.swarmradar.app.domain.model.DetectedObject
import com.swarmradar.app.domain.model.ObjectType
import com.swarmradar.app.domain.model.Point3D
import com.swarmradar.app.presentation.theme.ObjectColors

/**
 * Leichtgewichtiger Punktwolken-Renderer (Draufsicht-Projektion).
 * In Produktion durch Sceneform/OpenGL ES ersetzbar – hier bewusst ohne
 * zusätzliche Render-Dependency, damit der Build schlank bleibt.
 */
@Composable
fun PointCloudCanvas(
    points: List<Point3D>,
    objects: List<DetectedObject>,
    modifier: Modifier = Modifier
) {
    Canvas(modifier = modifier.fillMaxSize()) {
        val w = size.width
        val h = size.height
        val scale = 20f
        points.forEach { p ->
            val x = p.x * scale + w / 2f
            val y = p.z * scale + h / 2f
            drawCircle(Color(0x44AAAAFF), radius = 2f, center = Offset(x, y))
        }
        objects.forEach { o ->
            val color = when (o.type) {
                ObjectType.PERSON -> ObjectColors.PERSON
                ObjectType.ELECTRICAL -> ObjectColors.ELECTRICAL
                ObjectType.EXIT -> ObjectColors.EXIT
                else -> ObjectColors.REMOTE_DEVICE
            }
            drawCircle(
                color, radius = 8f,
                center = Offset(o.position.x.toFloat() * scale + w / 2f, o.position.z.toFloat() * scale + h / 2f)
            )
        }
    }
}
