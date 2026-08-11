package com.swarmradar.app.presentation.threeD

import android.content.Context
import android.view.ViewGroup
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.viewinterop.AndroidView
import com.google.ar.sceneform.Node
import com.google.ar.sceneform.SceneView
import com.google.ar.sceneform.math.Vector3 as SfVector3
import com.google.ar.sceneform.rendering.Color as SfColor
import com.google.ar.sceneform.rendering.Light
import com.google.ar.sceneform.rendering.Material
import com.google.ar.sceneform.rendering.MaterialFactory
import com.google.ar.sceneform.rendering.ShapeFactory
import com.swarmradar.app.domain.model.DetectedObject
import com.swarmradar.app.domain.model.ObjectType
import com.swarmradar.app.domain.model.Point3D
import com.swarmradar.app.presentation.theme.ObjectColors

private val RENDERER_KEY = Any()

/**
 * Sceneform-basierter 3D-Renderer (echtes 3D statt 2D-Canvas-Projektion).
 * Hostet eine [SceneView], baut Punktwolke + Objekte als Nodes auf und
 * hält die Materialien zwischen.
 */
class Scene3DRenderer(private val sceneView: SceneView) {
    private var pointMaterial: Material? = null
    private val objectMaterials = mutableMapOf<ObjectType, Material>()

    private var lastPoints: List<Point3D> = emptyList()
    private var lastObjects: List<DetectedObject> = emptyList()

    init {
        sceneView.scene.camera.setLocalPosition(SfVector3(0f, 1.5f, 5f))
        sceneView.scene.camera.lookAt(0f, 0f, 0f)
        sceneView.scene.addLight(
            Light.Builder(Light.Type.AMBIENT)
                .setColor(SfColor(1f, 1f, 1f))
                .setIntensity(1.3f)
                .build()
        )

        MaterialFactory.makeOpaqueWithColor(sceneView.context, SfColor(0.4f, 0.6f, 1f))
            .thenAccept { pointMaterial = it; sceneView.scene.callLater { rebuild() } }

        ObjectType.values().forEach { type ->
            val c = ObjectColors.colorFor(type)
            MaterialFactory.makeOpaqueWithColor(
                sceneView.context,
                SfColor(c.red, c.green, c.blue)
            ).thenAccept { objectMaterials[type] = it; sceneView.scene.callLater { rebuild() } }
        }
    }

    fun update(points: List<Point3D>, objects: List<DetectedObject>) {
        lastPoints = points
        lastObjects = objects
        rebuild()
    }

    private fun rebuild() {
        sceneView.scene.children
            .filterIsInstance<Node>()
            .forEach { sceneView.scene.removeChild(it) }

        pointMaterial?.let { mat ->
            val sphere = ShapeFactory.makeSphere(SfVector3(0.02f, 0.02f, 0.02f), SfVector3(0f, 0f, 0f), mat)
            lastPoints.forEach { p ->
                Node().apply {
                    setLocalPosition(SfVector3(p.x, p.y, p.z))
                    setRenderable(sphere)
                    sceneView.scene.addChild(this)
                }
            }
        }

        lastObjects.forEach { obj ->
            objectMaterials[obj.type]?.let { mat ->
                val sphere = ShapeFactory.makeSphere(SfVector3(0.15f, 0.15f, 0.15f), SfVector3(0f, 0f, 0f), mat)
                Node().apply {
                    setLocalPosition(
                        SfVector3(
                            obj.position.x.toFloat(),
                            obj.position.y.toFloat(),
                            obj.position.z.toFloat()
                        )
                    )
                    setRenderable(sphere)
                    sceneView.scene.addChild(this)
                }
            }
        }
    }
}

@Composable
fun SceneformView(
    points: List<Point3D>,
    objects: List<DetectedObject>,
    modifier: Modifier = Modifier.fillMaxSize()
) {
    AndroidView(
        modifier = modifier,
        factory = { ctx: Context ->
            SceneView(ctx).apply {
                layoutParams = ViewGroup.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT,
                    ViewGroup.LayoutParams.MATCH_PARENT
                )
            }
        },
        update = { view ->
            val renderer = view.getTag(RENDERER_KEY) as? Scene3DRenderer
                ?: Scene3DRenderer(view).also { view.setTag(RENDERER_KEY, it) }
            renderer.update(points, objects)
        },
        onRelease = { view -> view.destroy() }
    )
}
