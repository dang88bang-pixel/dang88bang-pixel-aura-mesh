package com.aura.agent.security

import org.json.JSONArray
import org.json.JSONObject
import java.security.MessageDigest
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

/**
 * Tamper-evident audit chain (IEC 62443 flavoured).
 *
 * `h_n = SHA256(h_{n-1} || canonicalJson(entry))`
 *
 * Mirrors `edge-agent/aura/audit.py`, so a chain produced on the CT45P
 * verifies on the edge agent and vice versa. Two details matter for that
 * compatibility, and both are easy to get wrong:
 *
 *  1. The digest covers a **canonical JSON** rendering with sorted keys and
 *     no spaces. Hashing `JSONObject.toString()` would depend on insertion
 *     order and break cross-platform verification.
 *  2. The timestamp is part of the hashed payload, not mixed in at hash time.
 *     A validator that folds `currentTimeMillis()` into the digest can never
 *     reproduce it later, silently making the whole log unverifiable.
 */
class CausalValidator(
    private val hmacKey: ByteArray? = null,
    private val genesis: String = GENESIS_HASH,
) {

    private val entriesInternal = mutableListOf<AuditEntry>()

    var lastHash: String = genesis
        private set

    val entries: List<AuditEntry> get() = entriesInternal.toList()
    val size: Int get() = entriesInternal.size

    @Synchronized
    fun append(
        actor: String,
        action: String,
        payload: JSONObject = JSONObject(),
        severity: Severity = Severity.INFO,
        timestamp: Long = System.currentTimeMillis(),
    ): AuditEntry {
        val entry = AuditEntry(
            index = entriesInternal.size,
            timestamp = timestamp,
            actor = actor,
            action = action,
            payload = payload,
            severity = severity,
            entryId = "aud-" + java.util.UUID.randomUUID().toString().replace("-", "").take(16),
            prevHash = lastHash,
        )
        entry.chainHash = digest(entry.prevHash, entry.hashableBody())
        entriesInternal += entry
        lastHash = entry.chainHash
        return entry
    }

    /** Recompute the whole chain. Reports the first bad index, or null when intact. */
    @Synchronized
    fun verify(): VerificationResult {
        var expectedPrev = genesis
        entriesInternal.forEachIndexed { position, entry ->
            if (entry.index != position) {
                return VerificationResult(false, position, "index mismatch at $position")
            }
            if (entry.prevHash != expectedPrev) {
                return VerificationResult(false, position, "broken link at $position")
            }
            val recomputed = digest(entry.prevHash, entry.hashableBody())
            if (!constantTimeEquals(recomputed, entry.chainHash)) {
                return VerificationResult(false, position, "payload at $position was modified")
            }
            expectedPrev = entry.chainHash
        }
        return VerificationResult(true, null, "chain of ${entriesInternal.size} entries is intact")
    }

    val isValid: Boolean get() = verify().valid

    @Synchronized
    fun tail(count: Int = 50, severity: Severity? = null): List<AuditEntry> {
        val filtered = if (severity == null) entriesInternal
        else entriesInternal.filter { it.severity == severity }
        return filtered.takeLast(count)
    }

    @Synchronized
    fun export(): JSONObject = JSONObject().apply {
        put("genesis", genesis)
        put("count", entriesInternal.size)
        put("head", lastHash)
        put("hmac", hmacKey != null)
        put("entries", JSONArray().also { array ->
            entriesInternal.forEach { array.put(it.toJson()) }
        })
    }

    @Synchronized
    fun restore(restored: List<AuditEntry>) {
        entriesInternal.clear()
        entriesInternal.addAll(restored)
        lastHash = entriesInternal.lastOrNull()?.chainHash ?: genesis
    }

    @Synchronized
    fun stats(): JSONObject {
        val result = verify()
        val bySeverity = JSONObject()
        Severity.values().forEach { severity ->
            val count = entriesInternal.count { it.severity == severity }
            if (count > 0) bySeverity.put(severity.wire, count)
        }
        return JSONObject().apply {
            put("count", entriesInternal.size)
            put("head", lastHash)
            put("valid", result.valid)
            put("message", result.message)
            put("by_severity", bySeverity)
            put("hmac_protected", hmacKey != null)
        }
    }

    internal fun digest(prevHash: String, body: String): String {
        val message = "$prevHash|$body".toByteArray(Charsets.UTF_8)
        return if (hmacKey != null) {
            val mac = Mac.getInstance("HmacSHA256")
            mac.init(SecretKeySpec(hmacKey, "HmacSHA256"))
            mac.doFinal(message).toHex()
        } else {
            MessageDigest.getInstance("SHA-256").digest(message).toHex()
        }
    }

    private fun constantTimeEquals(a: String, b: String): Boolean {
        if (a.length != b.length) return false
        var diff = 0
        for (i in a.indices) diff = diff or (a[i].code xor b[i].code)
        return diff == 0
    }

    companion object {
        const val GENESIS_HASH = "0000000000000000000000000000000000000000000000000000000000000000"

        fun ByteArray.toHex(): String = joinToString("") { "%02x".format(it) }

        /**
         * Deterministic JSON: keys sorted, no incidental whitespace.
         * Must match `aura.audit.canonical_json` exactly.
         */
        fun canonicalJson(value: Any?): String = when (value) {
            null, JSONObject.NULL -> "null"
            is JSONObject -> value.keys().asSequence().sorted().joinToString(
                separator = ",", prefix = "{", postfix = "}"
            ) { key -> "${quote(key)}:${canonicalJson(value.get(key))}" }
            is JSONArray -> (0 until value.length()).joinToString(
                separator = ",", prefix = "[", postfix = "]"
            ) { canonicalJson(value.get(it)) }
            is String -> quote(value)
            is Boolean -> value.toString()
            is Int, is Long -> value.toString()
            is Double, is Float -> {
                val d = (value as Number).toDouble()
                if (d == d.toLong().toDouble()) d.toLong().toString() else d.toString()
            }
            else -> quote(value.toString())
        }

        private fun quote(text: String): String = buildString {
            append('"')
            text.forEach { char ->
                when (char) {
                    '"' -> append("\\\"")
                    '\\' -> append("\\\\")
                    '\n' -> append("\\n")
                    '\r' -> append("\\r")
                    '\t' -> append("\\t")
                    else -> if (char < ' ') append("\\u%04x".format(char.code)) else append(char)
                }
            }
            append('"')
        }
    }
}

enum class Severity(val wire: String) {
    DEBUG("debug"),
    INFO("info"),
    NOTICE("notice"),
    WARNING("warning"),
    CRITICAL("critical"),
    SECURITY("security");

    companion object {
        fun fromWire(value: String): Severity = values().firstOrNull { it.wire == value } ?: INFO
    }
}

data class VerificationResult(
    val valid: Boolean,
    val firstBadIndex: Int?,
    val message: String,
)

data class AuditEntry(
    val index: Int,
    val timestamp: Long,
    val actor: String,
    val action: String,
    val payload: JSONObject,
    val severity: Severity,
    val entryId: String,
    val prevHash: String,
    var chainHash: String = "",
) {
    /** Exactly the bytes that go into the digest; hash fields are excluded. */
    fun hashableBody(): String {
        val body = JSONObject().apply {
            put("index", index)
            put("timestamp", timestamp / 1000.0)
            put("actor", actor)
            put("action", action)
            put("payload", payload)
            put("severity", severity.wire)
            put("entry_id", entryId)
        }
        return CausalValidator.canonicalJson(body)
    }

    fun toJson(): JSONObject = JSONObject().apply {
        put("index", index)
        put("timestamp", timestamp / 1000.0)
        put("actor", actor)
        put("action", action)
        put("payload", payload)
        put("severity", severity.wire)
        put("entry_id", entryId)
        put("prev_hash", prevHash)
        put("chain_hash", chainHash)
    }

    companion object {
        fun fromJson(json: JSONObject): AuditEntry = AuditEntry(
            index = json.getInt("index"),
            timestamp = (json.getDouble("timestamp") * 1000.0).toLong(),
            actor = json.getString("actor"),
            action = json.getString("action"),
            payload = json.optJSONObject("payload") ?: JSONObject(),
            severity = Severity.fromWire(json.optString("severity", "info")),
            entryId = json.getString("entry_id"),
            prevHash = json.getString("prev_hash"),
            chainHash = json.getString("chain_hash"),
        )
    }
}
