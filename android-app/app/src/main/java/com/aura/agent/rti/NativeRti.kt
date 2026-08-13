package com.aura.agent.rti

import java.io.Closeable

/**
 * Radio Tomographic Imaging over the BLE/Wi-Fi mesh.
 *
 * Reconstructs an attenuation image from per-link RSSI shadowing using the
 * native FISTA solver. The weight matrix is built once when the node layout is
 * known and cached natively - rebuilding it per frame would dominate the cost.
 *
 * Expect **1-2 m** localisation for a person with 10-16 handheld/portable
 * nodes. Sub-0.5 m figures in the literature assume 30+ nodes densely ringing
 * a single room. See `docs/performance_targets.md`.
 */
class NativeRti(
    private val nodes: List<RtiNode>,
    val minX: Float,
    val minY: Float,
    val resolution: Float,
    val nx: Int,
    val ny: Int,
    ellipseWidth: Float = 0.35f,
) : Closeable {

    private var handle: Long = 0
    private var closed = false

    /** Empty-room reference, averaged over the calibration samples. */
    private var baseline: FloatArray? = null
    private var baselineSamples = 0

    val linkCount: Int get() = if (handle != 0L) nativeGetLinkCount(handle) else 0
    val voxelCount: Int get() = nx * ny
    val isCalibrated: Boolean get() = baselineSamples > 0

    /** Deterministic link ordering: (0,1), (0,2), ... - must match the native side. */
    val links: List<Pair<String, String>> = buildList {
        for (i in nodes.indices) {
            for (j in i + 1 until nodes.size) add(nodes[i].id to nodes[j].id)
        }
    }

    private val linkIndex: Map<Pair<String, String>, Int> =
        links.withIndex().associate { (index, pair) -> pair to index }

    init {
        val flat = FloatArray(nodes.size * 2)
        nodes.forEachIndexed { i, node ->
            flat[2 * i] = node.x
            flat[2 * i + 1] = node.y
        }
        handle = nativeCreate(flat, minX, minY, resolution, nx, ny, ellipseWidth)
    }

    /** Order an RSSI map into the link vector the solver expects. */
    fun measurementVector(rssi: Map<Pair<String, String>, Float>): FloatArray {
        val out = FloatArray(links.size)
        rssi.forEach { (pair, value) ->
            val index = linkIndex[pair] ?: linkIndex[pair.second to pair.first]
            if (index != null) out[index] = value
        }
        return out
    }

    /** Accumulate an empty-room baseline (running mean). */
    fun calibrate(rssi: Map<Pair<String, String>, Float>): Int {
        val y = measurementVector(rssi)
        val current = baseline ?: FloatArray(links.size).also { baseline = it }
        baselineSamples++
        for (i in current.indices) current[i] += (y[i] - current[i]) / baselineSamples
        return baselineSamples
    }

    fun resetCalibration() {
        baseline = null
        baselineSamples = 0
    }

    /**
     * Reconstruct the attenuation image from a fresh RSSI snapshot.
     * Attenuation is the *drop* relative to the empty-room baseline.
     */
    fun reconstruct(
        rssi: Map<Pair<String, String>, Float>,
        alpha: Float = 0.08f,
        maxIterations: Int = 120,
        useL1: Boolean = true,
    ): FloatArray {
        if (handle == 0L) return FloatArray(0)
        val y = measurementVector(rssi)
        val reference = baseline
        val shadowing = FloatArray(y.size) { i ->
            if (reference == null) y[i] else (reference[i] - y[i]).coerceAtLeast(0f)
        }
        return nativeReconstruct(handle, shadowing, alpha, maxIterations, useL1) ?: FloatArray(0)
    }

    /** Flood-fill blobs above a fraction of the peak into weighted centroids. */
    fun extractTargets(image: FloatArray, thresholdRatio: Float = 0.45f): List<RtiTarget> {
        if (image.size != voxelCount) return emptyList()
        val peak = image.maxOrNull() ?: return emptyList()
        if (peak <= 1e-6f) return emptyList()

        val threshold = thresholdRatio * peak
        val visited = BooleanArray(voxelCount)
        val targets = mutableListOf<RtiTarget>()

        for (j in 0 until ny) {
            for (i in 0 until nx) {
                val index = j * nx + i
                if (visited[index] || image[index] <= threshold) continue
                val stack = ArrayDeque<Int>()
                stack.addLast(index)
                visited[index] = true
                var weight = 0f
                var sumX = 0f
                var sumY = 0f
                var maxValue = 0f
                var cells = 0

                while (stack.isNotEmpty()) {
                    val current = stack.removeLast()
                    val ci = current % nx
                    val cj = current / nx
                    val value = image[current]
                    weight += value
                    sumX += value * (minX + (ci + 0.5f) * resolution)
                    sumY += value * (minY + (cj + 0.5f) * resolution)
                    if (value > maxValue) maxValue = value
                    cells++

                    for (dj in -1..1) for (di in -1..1) {
                        val ni = ci + di
                        val nj = cj + dj
                        if (ni !in 0 until nx || nj !in 0 until ny) continue
                        val neighbour = nj * nx + ni
                        if (!visited[neighbour] && image[neighbour] > threshold) {
                            visited[neighbour] = true
                            stack.addLast(neighbour)
                        }
                    }
                }
                if (weight > 0f) {
                    targets += RtiTarget(sumX / weight, sumY / weight, maxValue, cells)
                }
            }
        }
        return targets.sortedByDescending { it.intensity }
    }

    fun lipschitz(): Float = if (handle != 0L) nativeGetLipschitz(handle) else 0f

    override fun close() {
        if (!closed && handle != 0L) {
            nativeDestroy(handle)
            handle = 0
            closed = true
        }
    }

    private external fun nativeCreate(
        nodes: FloatArray, minX: Float, minY: Float, resolution: Float,
        nx: Int, ny: Int, ellipseWidth: Float,
    ): Long
    private external fun nativeDestroy(handle: Long)
    private external fun nativeReconstruct(
        handle: Long, measurements: FloatArray, alpha: Float, maxIter: Int, useL1: Boolean,
    ): FloatArray?
    private external fun nativeGetLipschitz(handle: Long): Float
    private external fun nativeGetLinkCount(handle: Long): Int

    companion object {
        init { System.loadLibrary("aura_core") }
    }
}

data class RtiNode(val id: String, val x: Float, val y: Float)

data class RtiTarget(val x: Float, val y: Float, val intensity: Float, val voxels: Int)
