package com.aura.agent.ui

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.util.AttributeSet
import android.view.View
import kotlin.math.PI
import kotlin.math.cos
import kotlin.math.hypot
import kotlin.math.min
import kotlin.math.sin

/**
 * Artificial-horizon style attitude indicator.
 *
 * Drawn with plain Canvas rather than OpenGL: it updates at 20 Hz alongside a
 * WebGL map view in another tab, and a second GL context on this hardware
 * costs more than the whole 2D draw.
 */
class AttitudeView @JvmOverloads constructor(
    context: Context, attrs: AttributeSet? = null, defStyle: Int = 0,
) : View(context, attrs, defStyle) {

    private val skyPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.parseColor("#1B3A5C") }
    private val groundPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.parseColor("#4A3B2A") }
    private val linePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.parseColor("#E6ECF2")
        strokeWidth = 2f
        style = Paint.Style.STROKE
    }
    private val markerPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.parseColor("#00FF88")
        strokeWidth = 3f
        style = Paint.Style.STROKE
    }
    private val textPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.parseColor("#8A96A6")
        textSize = 26f
        textAlign = Paint.Align.CENTER
    }

    var roll: Float = 0f
    var pitch: Float = 0f
    var yaw: Float = 0f

    fun update(rollRad: Float, pitchRad: Float, yawRad: Float) {
        roll = rollRad
        pitch = pitchRad
        yaw = yawRad
        postInvalidateOnAnimation()
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        val cx = width / 2f
        val cy = height / 2f
        val radius = min(cx, cy) * 0.85f

        canvas.save()
        canvas.clipRect(cx - radius, cy - radius, cx + radius, cy + radius)
        canvas.rotate(Math.toDegrees(-roll.toDouble()).toFloat(), cx, cy)
        // 4 px of horizon travel per degree of pitch reads naturally at this size
        val horizon = cy + Math.toDegrees(pitch.toDouble()).toFloat() * 4f
        canvas.drawRect(cx - radius * 2, cy - radius * 2, cx + radius * 2, horizon, skyPaint)
        canvas.drawRect(cx - radius * 2, horizon, cx + radius * 2, cy + radius * 2, groundPaint)
        canvas.drawLine(cx - radius, horizon, cx + radius, horizon, linePaint)

        for (degrees in -30..30 step 10) {
            if (degrees == 0) continue
            val y = horizon - degrees * 4f
            val halfWidth = if (degrees % 20 == 0) radius * 0.35f else radius * 0.2f
            canvas.drawLine(cx - halfWidth, y, cx + halfWidth, y, linePaint)
        }
        canvas.restore()

        canvas.drawCircle(cx, cy, radius, linePaint)
        canvas.drawLine(cx - radius * 0.3f, cy, cx - radius * 0.08f, cy, markerPaint)
        canvas.drawLine(cx + radius * 0.08f, cy, cx + radius * 0.3f, cy, markerPaint)
        canvas.drawCircle(cx, cy, 4f, markerPaint)

        val heading = ((Math.toDegrees(yaw.toDouble()) + 360.0) % 360.0).toInt()
        canvas.drawText("%03d°".format(heading), cx, cy + radius + 30f, textPaint)
    }
}

/**
 * Top-down LiDAR sweep, drawn in the sensor body frame.
 *
 * Points are decimated to [maxPoints] before drawing: a 240-beam sweep at
 * 20 Hz is fine, but a 4000-point accumulated cloud would drop frames.
 */
class PointCloudView @JvmOverloads constructor(
    context: Context, attrs: AttributeSet? = null, defStyle: Int = 0,
) : View(context, attrs, defStyle) {

    private val pointPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.parseColor("#6A7A8A")
        style = Paint.Style.FILL
    }
    private val personPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.parseColor("#00FF88")
        style = Paint.Style.FILL
    }
    private val gridPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.parseColor("#1E2A38")
        strokeWidth = 1f
    }
    private val devicePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.parseColor("#2E7DD1")
        style = Paint.Style.FILL
    }

    private var angles: FloatArray = FloatArray(0)
    private var distances: FloatArray = FloatArray(0)
    private var people: List<Pair<Float, Float>> = emptyList()

    var rangeMeters = 10f
    var maxPoints = 720

    fun updateScan(newAngles: FloatArray, newDistances: FloatArray) {
        val count = min(newAngles.size, newDistances.size)
        val step = if (count > maxPoints) count / maxPoints else 1
        val kept = (0 until count step step).toList()
        angles = FloatArray(kept.size) { newAngles[kept[it]] }
        distances = FloatArray(kept.size) { newDistances[kept[it]] }
        postInvalidateOnAnimation()
    }

    fun updatePeople(positions: List<Pair<Float, Float>>) {
        people = positions
        postInvalidateOnAnimation()
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        val cx = width / 2f
        val cy = height / 2f
        val scale = min(cx, cy) / rangeMeters

        for (ring in 1..4) {
            canvas.drawCircle(cx, cy, ring * scale * rangeMeters / 4f, gridPaint)
        }
        canvas.drawLine(cx, 0f, cx, height.toFloat(), gridPaint)
        canvas.drawLine(0f, cy, width.toFloat(), cy, gridPaint)

        for (i in angles.indices) {
            val d = distances[i]
            if (d <= 0f || d > rangeMeters) continue
            // screen y grows downward, so negate to keep the map north-up
            val x = cx + d * cos(angles[i]) * scale
            val y = cy - d * sin(angles[i]) * scale
            canvas.drawCircle(x, y, 2f, pointPaint)
        }

        people.forEach { (px, py) ->
            if (hypot(px, py) > rangeMeters) return@forEach
            canvas.drawCircle(cx + px * scale, cy - py * scale, 7f, personPaint)
        }

        canvas.drawCircle(cx, cy, 6f, devicePaint)
    }
}

/** Horizontal RSSI strength bar for one BLE token. */
class RssiBarView @JvmOverloads constructor(
    context: Context, attrs: AttributeSet? = null, defStyle: Int = 0,
) : View(context, attrs, defStyle) {

    private val backgroundPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.parseColor("#141A24")
    }
    private val barPaint = Paint(Paint.ANTI_ALIAS_FLAG)
    private val labelPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.parseColor("#E6ECF2")
        textSize = 28f
    }

    var label: String = ""
    var rssi: Int = -100

    fun update(newLabel: String, newRssi: Int) {
        label = newLabel
        rssi = newRssi
        postInvalidateOnAnimation()
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        // -100 dBm (unusable) .. -40 dBm (very close)
        val fraction = ((rssi + 100) / 60f).coerceIn(0f, 1f)
        barPaint.color = when {
            fraction > 0.66f -> Color.parseColor("#00FF88")
            fraction > 0.33f -> Color.parseColor("#FFCC00")
            else -> Color.parseColor("#FF4444")
        }
        val barTop = height * 0.45f
        canvas.drawRect(0f, barTop, width.toFloat(), height.toFloat(), backgroundPaint)
        canvas.drawRect(0f, barTop, width * fraction, height.toFloat(), barPaint)
        canvas.drawText("$label   $rssi dBm", 4f, height * 0.35f, labelPaint)
    }
}
