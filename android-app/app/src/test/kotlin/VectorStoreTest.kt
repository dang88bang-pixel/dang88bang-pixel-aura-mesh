import com.aura.agent.llm.VectorEntry
import com.aura.agent.llm.VectorStore
import kotlin.math.abs

/**
 * Host-side tests for the RAG index the offline assistant retrieves from.
 *
 * The dangerous failure here is the same shape as the rest of the project: a
 * confident wrong number. A NaN similarity sorts to the top of a descending
 * ranking and would inject a garbage chunk into the prompt as if it were the
 * best match. These tests pin that it cannot.
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

private fun near(actual: Float, expected: Float, tol: Float, what: String) {
    checks++
    if (abs(actual - expected) > tol) {
        failures++
        println("  FAIL: $what (expected $expected +/- $tol, got $actual)")
    }
}

private fun section(name: String) = println("\n== $name ==")

private fun vec(vararg values: Float) = values

fun main() {
    println("AURA 6.0 - VectorStore test suite")

    section("cosine identity and orthogonality")
    near(VectorStore.cosine(vec(1f, 0f), vec(1f, 0f)), 1f, 1e-6f, "identical unit vectors")
    near(VectorStore.cosine(vec(1f, 0f), vec(0f, 1f)), 0f, 1e-6f, "orthogonal")
    near(VectorStore.cosine(vec(3f, 4f), vec(6f, 8f)), 1f, 1e-5f, "parallel, different length")
    near(VectorStore.cosine(vec(1f, 0f), vec(-1f, 0f)), -1f, 1e-6f, "opposite")

    section("degenerate input must score 0, never NaN")
    check(VectorStore.cosine(floatArrayOf(), vec(1f)) == 0f, "empty query")
    check(VectorStore.cosine(vec(1f), vec(1f, 2f)) == 0f, "mismatched length")
    check(VectorStore.cosine(vec(0f, 0f), vec(1f, 0f)) == 0f, "zero vector")
    val nan = VectorStore.cosine(vec(Float.NaN, 1f), vec(1f, 1f))
    check(nan == 0f, "NaN component scores 0, not $nan")
    val inf = VectorStore.cosine(vec(Float.POSITIVE_INFINITY, 1f), vec(1f, 1f))
    check(inf == 0f, "Inf component scores 0, not $inf")

    section("ranking prefers the closer chunk")
    val store = VectorStore()
    store.add(VectorEntry("a", "position good", vec(1f, 0f, 0f)))
    store.add(VectorEntry("b", "battery low", vec(0f, 1f, 0f)))
    store.add(VectorEntry("c", "smoke rising", vec(0f, 0f, 1f)))
    val hits = store.search(vec(0.9f, 0.1f, 0f), topK = 2)
    check(hits.size == 2, "topK respected")
    check(hits[0].entry.id == "a", "closest chunk is first (got ${hits.firstOrNull()?.entry?.id})")
    check(hits[0].score > hits[1].score, "scores are descending")

    section("replacing an id does not grow the store")
    store.add(VectorEntry("a", "position updated", vec(1f, 0f, 0f)))
    check(store.size == 3, "replace-in-place, size still 3 (got ${store.size})")
    check(store.texts().any { it.contains("updated") }, "new text replaced the old")

    section("empty query or empty store returns nothing, not a fabricated hit")
    check(store.search(floatArrayOf()).isEmpty(), "empty query")
    store.clear()
    check(store.size == 0, "cleared")
    check(store.search(vec(1f, 0f, 0f)).isEmpty(), "empty store")

    section("mutation: a NaN score must not win the ranking")
    // If cosine stopped refusing NaN, this entry would sort above every finite
    // score (NaN comparisons are false, so it survives a descending sort as
    // if it were the best). The store must not surface it first.
    store.add(VectorEntry("poison", "confabulated measurement", vec(Float.NaN, Float.NaN)))
    store.add(VectorEntry("real", "honest measurement", vec(1f, 0f)))
    val ranked = store.search(vec(1f, 0f), topK = 2)
    check(ranked.isNotEmpty(), "real entry is still retrievable")
    check(ranked[0].entry.id == "real", "NaN entry must not rank first (got ${ranked[0].entry.id})")
    check(ranked[0].score.isFinite(), "winning score is finite")

    println("\n----------------------------------------")
    println("$checks checks, $failures failures")
    if (failures > 0) kotlin.system.exitProcess(1)
}
