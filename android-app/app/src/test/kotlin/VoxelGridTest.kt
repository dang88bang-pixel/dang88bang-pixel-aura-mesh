import com.aura.agent.storage.ChunkAccumulator
import com.aura.agent.storage.ChunkKey
import com.aura.agent.storage.VoxelGrid

/**
 * Host-side tests for the voxel grid maths behind `VoxelChunkWriter`.
 *
 * The map must survive negative coordinates: truncation would collapse
 * x = -0.05 m and x = +0.05 m onto the same voxel and glue geometry together.
 * The accumulator's index layout must match the native codec's row-major,
 * z-fastest ordering, or every chunk written on the CT45P would decode as a
 * scrambled wall on the agent.
 */

private var checks = 0
private var failures = 0

private fun check(condition: Boolean, what: String) {
    checks++
    if (!condition) {
        failures++
        println("  FAIL: $what")
    }
}

private fun near(a: Float, b: Float): Boolean = kotlin.math.abs(a - b) < 1e-4f

fun main() {
    println("AURA 6.0 - voxel grid suite")

    // --- constants pinned to the codec / agent -------------------------
    // Numeric pins: this file must stay free of the JNI class
    // (NativeVoxelCodec) so it compiles without libaura_core on a host.
    // The agent-side chunking is CHUNK_SIZE=16 and LABEL_STRUCTURE=1.
    check(VoxelGrid.CHUNK_SIZE == 16, "CHUNK_SIZE matches NativeVoxelCodec/aura.voxel")
    check(VoxelGrid.LABEL_STRUCTURE == 1.toByte(), "LABEL_STRUCTURE matches the codec")

    // --- coordinate -> voxel (floor semantics) -------------------------
    check(VoxelGrid.voxel(0.0f, 0.1f) == 0, "origin is voxel 0")
    check(VoxelGrid.voxel(0.05f, 0.1f) == 0, "0.05 m rounds down to voxel 0")
    check(VoxelGrid.voxel(0.1f, 0.1f) == 1, "exactly 0.1 m is voxel 1")
    check(VoxelGrid.voxel(-0.01f, 0.1f) == -1, "-0.01 m is voxel -1, not 0")
    check(VoxelGrid.voxel(-0.1f, 0.1f) == -1, "-0.1 m is voxel -1")
    check(VoxelGrid.voxel(-1.0f, 0.1f) == -10, "-1.0 m is voxel -10")
    check(VoxelGrid.voxel(1.37f, 0.1f) == 13, "1.37 m is voxel 13")

    // --- voxel -> chunk (floor division for negatives) -----------------
    check(VoxelGrid.chunk(0) == 0, "voxel 0 is chunk 0")
    check(VoxelGrid.chunk(15) == 0, "voxel 15 is chunk 0")
    check(VoxelGrid.chunk(16) == 1, "voxel 16 is chunk 1")
    check(VoxelGrid.chunk(-1) == -1, "voxel -1 is chunk -1, not 0")
    check(VoxelGrid.chunk(-16) == -1, "voxel -16 is chunk -1")
    check(VoxelGrid.chunk(-17) == -2, "voxel -17 is chunk -2")

    // --- position inside the chunk -------------------------------------
    check(VoxelGrid.local(0) == 0, "voxel 0 is at chunk-local 0")
    check(VoxelGrid.local(15) == 15, "voxel 15 is at chunk-local 15")
    check(VoxelGrid.local(16) == 0, "voxel 16 wraps to chunk-local 0")
    check(VoxelGrid.local(-1) == 15, "voxel -1 is at chunk-local 15")
    check(VoxelGrid.local(-16) == 0, "voxel -16 is at chunk-local 0")
    check(VoxelGrid.local(-17) == 15, "voxel -17 is at chunk-local 15")
    check(
        VoxelGrid.chunk(17) * 16 + VoxelGrid.local(17) == 17,
        "chunk*16 + local reconstructs the voxel (positive)",
    )
    check(
        VoxelGrid.chunk(-17) * 16 + VoxelGrid.local(-17) == -17,
        "chunk*16 + local reconstructs the voxel (negative)",
    )

    // --- index layout: row-major, z-fastest ----------------------------
    check(VoxelGrid.index3d(0, 0, 0, 2) == 0, "origin is index 0")
    check(VoxelGrid.index3d(1, 0, 0, 2) == 1, "x is fastest")
    check(VoxelGrid.index3d(0, 1, 0, 2) == 2, "y steps by size")
    check(VoxelGrid.index3d(0, 0, 1, 2) == 4, "z steps by size^2")
    check(
        VoxelGrid.index3d(15, 15, 15, 16) == 16 * 16 * 16 - 1,
        "last voxel of a 16^3 chunk is the last index",
    )
    check(
        VoxelGrid.index3d(2, 3, 4, 16) == (4 * 16 + 3) * 16 + 2,
        "index formula matches (z*size + y)*size + x",
    )

    // --- accumulator semantics -----------------------------------------
    val acc = ChunkAccumulator(4)
    check(acc.occupied == 0, "fresh chunk has no occupied voxels")
    acc.add(1, 1, 1)
    acc.add(1, 1, 1)
    check(acc.occupied == 1, "repeat hit does not grow occupied count")
    check((acc.intensity[VoxelGrid.index3d(1, 1, 1, 4)].toInt() and 0xFFFF) == 2,
        "repeat hit grows the intensity")
    acc.add(2, 1, 1)
    check(acc.occupied == 2, "distinct voxel grows occupied count")
    check(acc.label[VoxelGrid.index3d(2, 1, 1, 4)] == 1.toByte(),
        "structure label is set on first hit")

    // saturation: 65535 hits must not wrap the counter
    val sat = ChunkAccumulator(2)
    for (i in 0..70_000) sat.add(0, 0, 0)
    check(
        (sat.intensity[0].toInt() and 0xFFFF) == 65_535,
        "intensity saturates at 65535 instead of wrapping",
    )

    // chunk key identity
    check(ChunkKey(1, 2, 3) == ChunkKey(1, 2, 3), "equal keys are equal")
    check(ChunkKey(1, 2, 3) != ChunkKey(1, 2, 4), "different keys differ")

    // one end-to-end geometry case: a wall straddling the origin lands in
    // two chunks with no voxel shared between them
    val left = VoxelGrid.voxel(-0.05f, 0.1f)
    val right = VoxelGrid.voxel(0.05f, 0.1f)
    check(left == -1 && right == 0, "wall at the origin hits voxels -1 and 0")
    check(
        VoxelGrid.chunk(left) != VoxelGrid.chunk(right),
        "opposite sides of the origin are different chunks",
    )

    println("\n----------------------------------------")
    println("$checks checks, $failures failures")
    if (failures > 0) kotlin.system.exitProcess(1)
}
