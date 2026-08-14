package com.aura.agent.storage

import kotlin.math.floor

/**
 * Pure voxel-grid geometry: world coordinate -> voxel index -> chunk index.
 *
 * No Android or JNI dependency, so the floor-division semantics for negative
 * coordinates are host-testable (`VoxelGridTest.kt`). The rules that matter:
 *
 *  * **Floor division everywhere.** A wall at x = -0.05 m with 0.1 m voxels is
 *    voxel -1, not 0 — truncation would collapse the two sides of the origin
 *    onto the same voxels and glue geometry together.
 *  * **Row-major, z-fastest** indexing matches the native codec and the edge
 *    agent's `aura.voxel` layout, so chunks sync without conversion.
 */
object VoxelGrid {

    /**
     * Chunk edge length in voxels. Must match `NativeVoxelCodec.CHUNK_SIZE`
     * and the edge agent's `aura.voxel` chunking; the equality is pinned in
     * `VoxelGridTest` (a numeric pin, because this file stays free of the
     * JNI class so it can compile on a host without `libaura_core`).
     */
    const val CHUNK_SIZE = 16

    /** Default label for structure hits; matches `NativeVoxelCodec.LABEL_STRUCTURE`. */
    const val LABEL_STRUCTURE: Byte = 1

    /** World metres -> voxel index (floor). */
    fun voxel(coord: Float, voxelSize: Float): Int =
        floor(coord / voxelSize).toInt()

    /** Voxel index -> chunk index (floor division; negatives round down). */
    fun chunk(voxel: Int, chunkSize: Int = CHUNK_SIZE): Int =
        Math.floorDiv(voxel, chunkSize)

    /** Position inside the chunk (0..chunkSize-1, floor semantics). */
    fun local(voxel: Int, chunkSize: Int = CHUNK_SIZE): Int =
        Math.floorMod(voxel, chunkSize)

    /** Row-major linear index into a chunk's flat arrays. */
    fun index3d(lx: Int, ly: Int, lz: Int, chunkSize: Int = CHUNK_SIZE): Int =
        (lz * chunkSize + ly) * chunkSize + lx
}

/**
 * In-memory accumulation for one chunk: hit counts (intensity) and a single
 * semantic label per voxel. Filled by [VoxelChunkWriter.ingest] and encoded
 * by `NativeVoxelCodec` on flush.
 */
class ChunkAccumulator(val size: Int = VoxelGrid.CHUNK_SIZE) {
    val intensity = ShortArray(size * size * size)
    val label = ByteArray(size * size * size)

    /** Number of distinct occupied voxels (label set, not hit count). */
    var occupied: Int = 0
        private set

    fun add(
        lx: Int,
        ly: Int,
        lz: Int,
        voxelLabel: Byte = VoxelGrid.LABEL_STRUCTURE,
    ) {
        val index = VoxelGrid.index3d(lx, ly, lz, size)
        val current = intensity[index].toInt() and 0xFFFF
        if (current == 0) {
            label[index] = voxelLabel
            occupied++
        }
        // Saturating counter: 65 535 hits is far beyond what a survey can
        // produce at 10 sweeps/s, but a wrap would corrupt the RLE runs.
        if (current < 0xFFFF) intensity[index] = (current + 1).toShort()
    }
}

data class ChunkKey(val cx: Int, val cy: Int, val cz: Int)
