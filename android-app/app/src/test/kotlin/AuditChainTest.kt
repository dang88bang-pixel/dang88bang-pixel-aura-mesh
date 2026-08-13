/**
 * Host-side tests for the Kotlin audit chain.
 *
 * These run on a plain JVM with kotlinc — no Android SDK, no Gradle, no
 * Robolectric — so the logic that decides whether a survey is admissible can
 * be verified in CI even where the Android toolchain is unavailable.
 *
 * The important test is [crossPlatformHashMatchesPython]: a chain written on a
 * CT45P must verify on the Python edge agent and vice versa. That only holds
 * if `canonical_json` produces the *same bytes* on both sides, which is easy
 * to get subtly wrong (key order, float rendering, escaping).
 *
 * Build and run:
 *   tools/run-kotlin-tests.sh
 */

import com.aura.agent.security.AuditEntry
import com.aura.agent.security.CausalValidator
import com.aura.agent.security.Severity
import org.json.JSONObject
import java.security.MessageDigest

private var checks = 0
private var failures = 0

private fun check(condition: Boolean, what: String) {
    checks++
    if (!condition) {
        failures++
        println("  FAIL: $what")
    }
}

private fun checkEquals(actual: Any?, expected: Any?, what: String) {
    checks++
    if (actual != expected) {
        failures++
        println("  FAIL: $what")
        println("        expected: $expected")
        println("        actual:   $actual")
    }
}

private fun section(name: String) = println("\n== $name ==")

private fun sha256Hex(text: String): String =
    MessageDigest.getInstance("SHA-256").digest(text.toByteArray(Charsets.UTF_8))
        .joinToString("") { "%02x".format(it) }

// ---------------------------------------------------------------------
private fun canonicalJsonTests() {
    section("canonical JSON")

    val a = JSONObject().put("b", 1).put("a", 2)
    val b = JSONObject().put("a", 2).put("b", 1)
    checkEquals(
        CausalValidator.canonicalJson(a),
        CausalValidator.canonicalJson(b),
        "key order must not affect the canonical form",
    )
    checkEquals(CausalValidator.canonicalJson(a), """{"a":2,"b":1}""", "keys are sorted, no spaces")

    // Python's json.dumps keeps the trailing .0 on integral floats. Kotlin
    // must do the same or no timestamp will ever hash identically on the two
    // platforms. Pinned against real Python output below.
    checkEquals(
        CausalValidator.canonicalJson(JSONObject().put("v", 5.0)),
        """{"v":5.0}""",
        "integral doubles KEEP the .0, matching Python's json.dumps",
    )
    checkEquals(
        CausalValidator.canonicalJson(JSONObject().put("v", 1.5)),
        """{"v":1.5}""",
        "non-integral doubles keep their fraction",
    )
    checkEquals(
        CausalValidator.canonicalJson(JSONObject().put("s", "a\"b\\c")),
        """{"s":"a\"b\\c"}""",
        "quotes and backslashes are escaped",
    )
    checkEquals(
        CausalValidator.canonicalJson(JSONObject().put("s", "line\nbreak")),
        """{"s":"line\nbreak"}""",
        "newlines are escaped",
    )
    checkEquals(
        CausalValidator.canonicalJson(JSONObject().put("nested", JSONObject().put("z", 1).put("y", 2))),
        """{"nested":{"y":2,"z":1}}""",
        "nested objects are sorted recursively",
    )
}

// ---------------------------------------------------------------------
private fun chainTests() {
    section("chain integrity")

    val validator = CausalValidator()
    repeat(10) { i ->
        validator.append("ct45p", "scan.start", JSONObject().put("i", i))
    }

    val result = validator.verify()
    check(result.valid, "a freshly built chain verifies")
    checkEquals(result.firstBadIndex, null, "no bad index on an intact chain")
    checkEquals(validator.size, 10, "all entries retained")
    checkEquals(validator.entries[0].prevHash, CausalValidator.GENESIS_HASH, "first entry links to genesis")

    for (i in 1 until validator.size) {
        check(
            validator.entries[i].prevHash == validator.entries[i - 1].chainHash,
            "entry $i links to entry ${i - 1}",
        )
    }

    // tamper with a payload
    validator.entries[3].payload.put("i", 999)
    val tampered = validator.verify()
    check(!tampered.valid, "payload tampering is detected")
    checkEquals(tampered.firstBadIndex, 3, "tampering is localised to the right index")
    check(tampered.message.contains("modified"), "message names the failure mode")
}

private fun tamperVariantTests() {
    section("tamper variants")

    // deletion
    val deletion = CausalValidator()
    repeat(6) { deletion.append("a", "e", JSONObject().put("i", it)) }
    val entries = deletion.entries.toMutableList()
    entries.removeAt(2)
    deletion.restore(entries)
    val afterDelete = deletion.verify()
    check(!afterDelete.valid, "deleting an entry breaks the chain")
    checkEquals(afterDelete.firstBadIndex, 2, "deletion detected at the gap")

    // reordering
    val reorder = CausalValidator()
    repeat(5) { reorder.append("a", "e", JSONObject().put("i", it)) }
    val swapped = reorder.entries.toMutableList()
    val tmp = swapped[1]
    swapped[1] = swapped[2]
    swapped[2] = tmp
    reorder.restore(swapped)
    check(!reorder.verify().valid, "reordering is detected")

    // truncation is NOT detected by the chain alone - the remaining prefix is
    // internally consistent. This is a real property of hash chains and the
    // reason the head hash must be anchored externally.
    val truncate = CausalValidator()
    repeat(8) { truncate.append("a", "e", JSONObject().put("i", it)) }
    val head = truncate.lastHash
    truncate.restore(truncate.entries.take(4))
    check(truncate.verify().valid, "a truncated prefix still verifies internally (expected)")
    check(truncate.lastHash != head, "but the head hash changed, which an external anchor catches")
}

private fun hmacTests() {
    section("HMAC chain")

    val keyed = CausalValidator(hmacKey = "super-secret".toByteArray())
    repeat(4) { keyed.append("ct45p", "event", JSONObject().put("i", it)) }
    check(keyed.verify().valid, "HMAC chain verifies with the key")

    // An attacker without the key rewrites an entry and recomputes the digest
    // using a plain SHA-256 validator.
    val forger = CausalValidator()
    keyed.entries[2].payload.put("i", 42)
    keyed.entries[2].chainHash = forger.digest(keyed.entries[2].prevHash, keyed.entries[2].hashableBody())
    check(!keyed.verify().valid, "a forged entry is rejected without the key")

    // Same content, different key -> different hash
    val k1 = CausalValidator(hmacKey = "key-one".toByteArray())
    val k2 = CausalValidator(hmacKey = "key-two".toByteArray())
    val e1 = k1.append("a", "b", JSONObject().put("x", 1), timestamp = 1000L)
    val e2 = k2.append("a", "b", JSONObject().put("x", 1), timestamp = 1000L)
    check(e1.chainHash != e2.chainHash, "different keys produce different chains")
}

private fun serialisationTests() {
    section("export / restore")

    val original = CausalValidator()
    repeat(5) { original.append("ct45p", "event", JSONObject().put("i", it), timestamp = 1000L + it) }
    val exported = original.export()
    checkEquals(exported.getInt("count"), 5, "export records the count")
    checkEquals(exported.getString("head"), original.lastHash, "export records the head")

    val restored = CausalValidator()
    val array = exported.optJSONArray("entries")!!
    restored.restore((0 until array.length()).map { AuditEntry.fromJson(array.getJSONObject(it)) })
    check(restored.verify().valid, "a round-tripped chain still verifies")
    checkEquals(restored.lastHash, original.lastHash, "head survives the round trip")
}

private fun severityTests() {
    section("severity")

    val validator = CausalValidator()
    validator.append("a", "x", severity = Severity.SECURITY)
    validator.append("b", "y", severity = Severity.INFO)
    validator.append("a", "z", severity = Severity.SECURITY)

    checkEquals(validator.tail(severity = Severity.SECURITY).size, 2, "severity filter works")
    checkEquals(Severity.fromWire("security"), Severity.SECURITY, "wire round trip")
    checkEquals(Severity.fromWire("nonsense"), Severity.INFO, "unknown severity falls back to info")

    val stats = validator.stats()
    checkEquals(stats.getInt("count"), 3, "stats count")
    check(stats.getBoolean("valid"), "stats reports validity")
}

// ---------------------------------------------------------------------
/**
 * The cross-platform contract.
 *
 * These digests were produced by the Python implementation
 * (`edge-agent/aura/audit.py`) with fixed inputs. If the Kotlin canonical form
 * ever drifts — key ordering, float rendering, escaping — these fail, and an
 * audit chain written on the handheld would no longer verify on the agent.
 *
 * Regenerate with `tools/generate_audit_fixtures.py`.
 */
private fun crossPlatformHashMatchesPython() {
    section("cross-platform: Kotlin must match Python")

    data class Fixture(val body: String, val prevHash: String, val expected: String)

    // canonical_json of the hashable body, exactly as Python emits it
    val fixtures = listOf(
        Fixture(
            body = """{"action":"scan.start","actor":"ct45p","entry_id":"aud-0000000000000001","index":0,"payload":{"i":0},"severity":"info","timestamp":1000.0}""",
            prevHash = CausalValidator.GENESIS_HASH,
            expected = PYTHON_DIGEST_0,
        ),
        Fixture(
            body = """{"action":"marker.place","actor":"operator","entry_id":"aud-0000000000000002","index":1,"payload":{"x":3.5,"y":4.0},"severity":"security","timestamp":1000.5}""",
            prevHash = PYTHON_DIGEST_0,
            expected = PYTHON_DIGEST_1,
        ),
    )

    val validator = CausalValidator()
    fixtures.forEachIndexed { index, fixture ->
        val actual = validator.digest(fixture.prevHash, fixture.body)
        checkEquals(actual, fixture.expected, "fixture $index digest matches Python")
    }

    // Independently: the digest must be plain SHA-256 over "prev|body".
    val manual = sha256Hex("${CausalValidator.GENESIS_HASH}|${fixtures[0].body}")
    checkEquals(manual, fixtures[0].expected, "digest is SHA256(prev|canonical_json)")

    // And the canonical form the Kotlin side builds for the same entry must
    // equal the Python body byte for byte.
    val entry = AuditEntry(
        index = 0,
        timestamp = 1_000_000L,          // 1000.0 s
        actor = "ct45p",
        action = "scan.start",
        payload = JSONObject().put("i", 0),
        severity = Severity.INFO,
        entryId = "aud-0000000000000001",
        prevHash = CausalValidator.GENESIS_HASH,
    )
    checkEquals(entry.hashableBody(), fixtures[0].body, "Kotlin canonical body matches Python byte for byte")
}

// Digests computed by edge-agent/aura/audit.py - see tools/generate_audit_fixtures.py
private const val PYTHON_DIGEST_0 = "@DIGEST0@"
private const val PYTHON_DIGEST_1 = "@DIGEST1@"

// ---------------------------------------------------------------------
fun main() {
    println("AURA 6.0 - Kotlin audit chain test suite")

    canonicalJsonTests()
    chainTests()
    tamperVariantTests()
    hmacTests()
    serialisationTests()
    severityTests()
    crossPlatformHashMatchesPython()

    println("\n----------------------------------------")
    println("$checks checks, $failures failures")
    if (failures > 0) kotlin.system.exitProcess(1)
}
