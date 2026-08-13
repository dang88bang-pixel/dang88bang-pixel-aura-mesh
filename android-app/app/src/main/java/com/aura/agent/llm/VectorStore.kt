package com.aura.agent.llm

import kotlin.math.sqrt

data class VectorEntry(
    val id: String,
    val text: String,
    val embedding: FloatArray,
    val metadata: Map<String, String> = emptyMap(),
) {
    override fun equals(other: Any?): Boolean = other is VectorEntry && id == other.id
    override fun hashCode(): Int = id.hashCode()
}

data class SearchHit(val entry: VectorEntry, val score: Float) {
    val text: String get() = entry.text
}

/**
 * In-memory cosine-similarity index.
 *
 * A brute-force scan is the right choice here: a survey produces hundreds of
 * chunks, not millions, and an ANN index would cost more in complexity and
 * recall than it saves in microseconds.
 *
 * Kept free of Android types so the host-JVM suite can compile it the same
 * way it compiles [com.aura.agent.sensors.UwbGeometry].
 */
class VectorStore {
    private val entries = mutableListOf<VectorEntry>()

    val size: Int get() = entries.size

    @Synchronized
    fun add(entry: VectorEntry) {
        entries.removeAll { it.id == entry.id }
        entries += entry
    }

    @Synchronized
    fun search(query: FloatArray, topK: Int = 5): List<SearchHit> {
        if (query.isEmpty() || entries.isEmpty()) return emptyList()
        return entries
            .map { SearchHit(it, cosine(query, it.embedding)) }
            .sortedByDescending { it.score }
            .take(topK)
    }

    @Synchronized
    fun clear() = entries.clear()

    /** Texts currently held, for the no-model fallback in the UI. */
    @Synchronized
    fun texts(): List<String> = entries.map { it.text }

    companion object {
        /**
         * Cosine similarity. Zero for empty, mismatched, or degenerate vectors
         * — never NaN. A NaN score would sort to the top and surface a
         * nonsense hit as the best match.
         */
        fun cosine(a: FloatArray, b: FloatArray): Float {
            if (a.size != b.size || a.isEmpty()) return 0f
            var dot = 0.0
            var normA = 0.0
            var normB = 0.0
            for (i in a.indices) {
                val ai = a[i].toDouble()
                val bi = b[i].toDouble()
                if (!ai.isFinite() || !bi.isFinite()) return 0f
                dot += ai * bi
                normA += ai * ai
                normB += bi * bi
            }
            val denominator = sqrt(normA) * sqrt(normB)
            return if (denominator < 1e-9) 0f else (dot / denominator).toFloat()
        }
    }
}
