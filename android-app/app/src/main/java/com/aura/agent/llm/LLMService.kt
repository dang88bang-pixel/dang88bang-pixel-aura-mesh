package com.aura.agent.llm

import android.content.Context
import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.File
import java.io.FileOutputStream
import kotlin.math.sqrt

private const val TAG = "AuraLLM"

/**
 * Offline assistant: Phi-3-mini (GGUF, Q4_K_M) via llama.cpp + a local RAG
 * store, so the device answers questions about a survey with no network.
 *
 * Reality check on the CT45P-X0N (Snapdragon QCS4290, 4 GB RAM):
 *
 * | model            | quant  | file   | RAM   | tokens/s |
 * |------------------|--------|--------|-------|----------|
 * | Phi-3-mini 3.8B  | Q4_K_M | 2.4 GB | ~2.8G | 3-6      |
 * | Phi-3-mini 3.8B  | Q4_0   | 2.2 GB | ~2.6G | 4-7      |
 * | Qwen2.5 1.5B     | Q4_K_M | 1.0 GB | ~1.3G | 10-16    |
 *
 * The often-quoted ">10 t/s for Phi-3" figure comes from 8-core flagship SoCs
 * with much higher memory bandwidth. On a 4 GB device Phi-3 also competes with
 * the sensor pipeline for RAM, so **Qwen2.5-1.5B-Instruct is the default** and
 * Phi-3 is opt-in. See `docs/performance_targets.md`.
 */
class LLMService(private val context: Context) {

    private var handle: Long = 0
    private val vectorStore = VectorStore()

    var modelName: String = ""
        private set

    val isLoaded: Boolean get() = handle != 0L

    /**
     * Load a GGUF model from app assets or external storage.
     *
     * @param threads inference threads; leave one core for the fusion loop,
     *                otherwise the UI visibly stutters during generation.
     */
    suspend fun load(
        modelFileName: String = DEFAULT_MODEL,
        threads: Int = (Runtime.getRuntime().availableProcessors() - 1).coerceAtLeast(2),
        contextSize: Int = 2048,
    ): Boolean = withContext(Dispatchers.IO) {
        val modelFile = resolveModel(modelFileName) ?: run {
            Log.w(TAG, "model $modelFileName not found - assistant disabled")
            return@withContext false
        }
        val available = Runtime.getRuntime().maxMemory()
        Log.i(TAG, "loading ${modelFile.name} (${modelFile.length() / 1_048_576} MB), heap ${available / 1_048_576} MB")

        handle = runCatching { nativeInit(modelFile.absolutePath, threads, contextSize) }
            .getOrElse {
                Log.e(TAG, "llama.cpp init failed", it)
                0L
            }
        if (handle != 0L) modelName = modelFile.name
        handle != 0L
    }

    /** Plain completion, no retrieval. */
    suspend fun generate(prompt: String, maxTokens: Int = 256): String = withContext(Dispatchers.IO) {
        if (!isLoaded) return@withContext "Modell nicht geladen."
        runCatching { nativeGenerate(handle, prompt, maxTokens) }
            .getOrElse { "Fehler bei der Inferenz: ${it.message}" }
    }

    /**
     * Retrieval-augmented answer over the indexed survey context.
     *
     * The retrieved chunks are injected verbatim and the model is instructed
     * to stay within them, which is what keeps a 1.5-3.8B model from
     * confabulating measurements it never saw.
     */
    suspend fun ask(question: String, topK: Int = 4): String = withContext(Dispatchers.IO) {
        if (!isLoaded) return@withContext "Modell nicht geladen."
        val queryEmbedding = embed(question)
        val hits = vectorStore.search(queryEmbedding, topK)
        val contextBlock = if (hits.isEmpty()) "(kein Kontext indiziert)"
        else hits.joinToString("\n---\n") { it.text }

        val prompt = """
            <|system|>
            Du bist der Analyse-Assistent der AURA-Plattform. Antworte ausschliesslich
            auf Basis des folgenden Kontexts. Wenn der Kontext die Frage nicht
            beantwortet, sage das offen. Erfinde keine Messwerte.<|end|>
            <|user|>
            Kontext:
            $contextBlock

            Frage: $question<|end|>
            <|assistant|>
        """.trimIndent()

        generate(prompt, maxTokens = 320)
    }

    fun embed(text: String): FloatArray {
        if (!isLoaded) return FloatArray(0)
        return runCatching { nativeEmbed(handle, text) }.getOrDefault(FloatArray(0))
    }

    /** Index a piece of survey context (a scan summary, a scenario result...). */
    fun index(id: String, text: String, metadata: Map<String, String> = emptyMap()) {
        val embedding = embed(text)
        if (embedding.isEmpty()) return
        vectorStore.add(VectorEntry(id, text, embedding, metadata))
    }

    fun indexedCount(): Int = vectorStore.size

    fun clearIndex() = vectorStore.clear()

    fun unload() {
        if (handle != 0L) {
            runCatching { nativeFree(handle) }
            handle = 0
            modelName = ""
        }
    }

    private fun resolveModel(fileName: String): File? {
        // 1) already extracted to internal storage
        val internal = File(context.filesDir, fileName)
        if (internal.exists() && internal.length() > 0) return internal

        // 2) side-loaded next to the app's external files (typical for a
        //    2.4 GB model that must not bloat the APK)
        context.getExternalFilesDir(null)?.let { dir ->
            val external = File(dir, fileName)
            if (external.exists() && external.length() > 0) return external
        }

        // 3) bundled in assets - extract once
        return runCatching {
            context.assets.open(fileName).use { input ->
                FileOutputStream(internal).use { output -> input.copyTo(output, 1 shl 20) }
            }
            internal
        }.getOrNull()
    }

    private external fun nativeInit(modelPath: String, threads: Int, contextSize: Int): Long
    private external fun nativeGenerate(handle: Long, prompt: String, maxTokens: Int): String
    private external fun nativeEmbed(handle: Long, text: String): FloatArray
    private external fun nativeFree(handle: Long)

    companion object {
        /** Small enough to coexist with the sensor pipeline on a 4 GB device. */
        const val DEFAULT_MODEL = "qwen2.5-1.5b-instruct-q4_k_m.gguf"
        const val PHI3_MODEL = "phi-3-mini-4k-instruct-q4_k_m.gguf"

        init {
            // Optional: the app runs fine without the LLM library present.
            runCatching { System.loadLibrary("llama_bridge") }
                .onFailure { Log.w(TAG, "llama_bridge unavailable - assistant disabled") }
        }
    }
}

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

    private fun cosine(a: FloatArray, b: FloatArray): Float {
        if (a.size != b.size || a.isEmpty()) return 0f
        var dot = 0.0
        var normA = 0.0
        var normB = 0.0
        for (i in a.indices) {
            dot += a[i] * b[i]
            normA += a[i] * a[i]
            normB += b[i] * b[i]
        }
        val denominator = sqrt(normA) * sqrt(normB)
        return if (denominator < 1e-9) 0f else (dot / denominator).toFloat()
    }
}
