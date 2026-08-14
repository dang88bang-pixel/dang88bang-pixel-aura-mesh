package com.aura.agent.storage

/**
 * Folds projected LiDAR sweeps into the persistent chunked voxel map.
 *
 * The sweep is 2D (x, y pairs) at the device height; [ingest] lifts it to a
 * single z layer. Occupancy is accumulated in memory across sweeps so a wall
 * seen on ten sweeps carries a higher intensity than a ghost seen once, and
 * every touched chunk is RLE-encoded via [NativeVoxelCodec] and persisted
 * through [LocalVectorStore.saveChunk] on [flush].
 *
 * Chunks are persisted at most once per second (caller-driven): the codec is
 * cheap but SQLite writes are not, and at 10 sweeps/s the WAL would grow
 * faster than the retention job trims it.
 *
 * The geometry (floor division for negative coordinates, chunk membership)
 * lives in [VoxelGrid] and is host-tested; this class is the thin glue
 * between that pure maths and the store/codec.
 */
class VoxelChunkWriter(
    private val store: LocalVectorStore,
    private val voxelSize: Float = 0.1f,
    private val chunkSize: Int = VoxelGrid.CHUNK_SIZE,
) {
    /** Chunks that have changed since the last flush. */
    private val dirty = LinkedHashSet<ChunkKey>()

    /** Full per-chunk accumulators, bounded so a long survey stays flat. */
    private val pending = LinkedHashMap<ChunkKey, ChunkAccumulator>()

    /** Fold a projected sweep (`x0, y0, x1, y1, ...`) into the map at height [z]. */
    fun ingest(points: FloatArray, z: Float) {
        val vz = VoxelGrid.voxel(z, voxelSize)
        var i = 0
        while (i + 1 < points.size) {
            val vx = VoxelGrid.voxel(points[i], voxelSize)
            val vy = VoxelGrid.voxel(points[i + 1], voxelSize)
            val key = ChunkKey(
                VoxelGrid.chunk(vx, chunkSize),
                VoxelGrid.chunk(vy, chunkSize),
                VoxelGrid.chunk(vz, chunkSize),
            )
            pending.getOrPut(key) { ChunkAccumulator(chunkSize) }.add(
                VoxelGrid.local(vx, chunkSize),
                VoxelGrid.local(vy, chunkSize),
                VoxelGrid.local(vz, chunkSize),
            )
            dirty.add(key)
            i += 2
        }
        // Bounded memory: once the window covers more than this many chunks
        // (e.g. a whole floor), persist and reset. The map stays correct —
        // only the hit-count history is reset, exactly like the agent's
        // occupancy decay.
        if (pending.size >= MAX_PENDING_CHUNKS) flush()
    }

    /**
     * Encode and persist every chunk touched since the last flush.
     *
     * @return number of chunks written; 0 when nothing changed.
     */
    fun flush(): Int {
        var written = 0
        for (key in dirty) {
            val acc = pending[key] ?: continue
            val blob = NativeVoxelCodec.encode(acc.intensity, acc.label, acc.size) ?: continue
            store.saveChunk(key.cx, key.cy, key.cz, blob)
            written++
        }
        dirty.clear()
        if (pending.size >= MAX_PENDING_CHUNKS) pending.clear()
        return written
    }

    /** Chunks currently held in memory (diagnostics). */
    fun pendingChunkCount(): Int = pending.size

    companion object {
        /** ~2 048 chunks × 12 KiB ≈ 24 MiB worst case; see [ingest]. */
        const val MAX_PENDING_CHUNKS = 2048
    }
}
