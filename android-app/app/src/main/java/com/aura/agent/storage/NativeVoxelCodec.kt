package com.aura.agent.storage

/**
 * RLE codec for voxel chunks, binary-compatible with `aura.voxel` on the
 * edge agent, so chunks sync in either direction without conversion.
 *
 * Layout: 'AVX1' | uint8 size | uint32 runs | runs * (u16 count, u16 intensity, u8 label)
 */
object NativeVoxelCodec {

    const val CHUNK_SIZE = 16

    // Semantic labels - keep in sync with res/values/colors.xml and palette.js
    const val LABEL_EMPTY: Byte = 0
    const val LABEL_STRUCTURE: Byte = 1
    const val LABEL_PERSON: Byte = 2
    const val LABEL_DEVICE: Byte = 3
    const val LABEL_HAZARD: Byte = 4
    const val LABEL_EXIT: Byte = 5

    fun encode(intensity: ShortArray, label: ByteArray, size: Int = CHUNK_SIZE): ByteArray? =
        nativeEncode(intensity, label, size)

    fun decode(blob: ByteArray, size: Int = CHUNK_SIZE): DecodedChunk? {
        val total = size * size * size
        val intensity = ShortArray(total)
        val label = ByteArray(total)
        return if (nativeDecode(blob, intensity, label, size)) DecodedChunk(intensity, label, size)
        else null
    }

    private external fun nativeEncode(intensity: ShortArray, label: ByteArray, size: Int): ByteArray?
    private external fun nativeDecode(
        blob: ByteArray, intensityOut: ShortArray, labelOut: ByteArray, size: Int,
    ): Boolean

    init { System.loadLibrary("aura_core") }
}

data class DecodedChunk(val intensity: ShortArray, val label: ByteArray, val size: Int) {
    fun at(x: Int, y: Int, z: Int): Pair<Int, Byte> {
        val index = (z * size + y) * size + x
        return (intensity[index].toInt() and 0xFFFF) to label[index]
    }

    override fun equals(other: Any?): Boolean =
        other is DecodedChunk && size == other.size && intensity.contentEquals(other.intensity)

    override fun hashCode(): Int = 31 * size + intensity.contentHashCode()
}
