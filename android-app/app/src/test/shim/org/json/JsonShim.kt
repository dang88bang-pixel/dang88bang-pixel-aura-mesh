@file:JvmName("JsonShim")

package org.json

/**
 * Minimal `org.json` shim for host-side (JVM) unit tests.
 *
 * Android bundles `org.json` in the platform, so it is a compile-only symbol
 * that is **not** on the classpath for plain JVM tests. The usual answers are
 * Robolectric or the Maven `org.json:json` artifact; this shim exists so the
 * pure-logic classes — `CausalValidator` above all — can be compiled and
 * executed with nothing but a JRE and kotlinc.
 *
 * It implements only what those classes touch, but it is faithful in the two
 * places that decide whether an audit chain verifies across platforms:
 *
 *  * **Insertion order is preserved** (`LinkedHashMap`). Real org.json uses a
 *    `HashMap`, so key order there is arbitrary — which is exactly why
 *    `CausalValidator.canonicalJson` sorts keys itself instead of trusting
 *    `toString()`.
 *  * **Doubles render exactly like Python's `json.dumps`**, i.e. integral
 *    values keep their trailing `.0`. Get this wrong and every hash computed
 *    on-device differs from the agent's — every timestamp is a float.
 *
 * Test scope only; never shipped in the APK.
 */
class JSONObject {

    private val values = LinkedHashMap<String, Any?>()

    constructor()

    constructor(source: Map<*, *>) {
        source.forEach { (key, value) -> values[key.toString()] = value }
    }

    fun put(key: String, value: Any?): JSONObject {
        values[key] = value
        return this
    }

    fun put(key: String, value: Int): JSONObject = put(key, value as Any)
    fun put(key: String, value: Long): JSONObject = put(key, value as Any)
    fun put(key: String, value: Double): JSONObject = put(key, value as Any)
    fun put(key: String, value: Boolean): JSONObject = put(key, value as Any)

    fun get(key: String): Any =
        values[key] ?: throw JSONException("no value for $key")

    fun opt(key: String): Any? = values[key]

    fun getString(key: String): String = get(key).toString()

    fun optString(key: String, fallback: String = ""): String =
        values[key]?.toString() ?: fallback

    fun getInt(key: String): Int = (get(key) as Number).toInt()
    fun getLong(key: String): Long = (get(key) as Number).toLong()
    fun getDouble(key: String): Double = (get(key) as Number).toDouble()
    fun getBoolean(key: String): Boolean = get(key) as Boolean

    fun optJSONObject(key: String): JSONObject? = values[key] as? JSONObject
    fun optJSONArray(key: String): JSONArray? = values[key] as? JSONArray

    fun has(key: String): Boolean = values.containsKey(key)
    fun length(): Int = values.size
    fun keys(): Iterator<String> = values.keys.iterator()

    override fun toString(): String = buildString {
        append('{')
        var first = true
        values.forEach { (key, value) ->
            if (!first) append(',')
            first = false
            append(quote(key)).append(':').append(render(value))
        }
        append('}')
    }

    companion object {
        @JvmField
        val NULL: Any = object : Any() {
            override fun equals(other: Any?): Boolean = other == null || other === this
            override fun hashCode(): Int = 0
            override fun toString(): String = "null"
        }

        internal fun render(value: Any?): String = when {
            value == null || value === NULL -> "null"
            value is JSONObject || value is JSONArray -> value.toString()
            value is Boolean -> value.toString()
            value is Double || value is Float -> {
                // Match Python's json.dumps: integral floats KEEP the ".0".
                // (Real org.json collapses them, which is one of the reasons
                // CausalValidator never relies on toString() for hashing.)
                (value as Number).toDouble().toString()
            }
            value is Number -> value.toString()
            else -> quote(value.toString())
        }

        internal fun quote(text: String): String = buildString {
            append('"')
            text.forEach { c ->
                when (c) {
                    '"' -> append("\\\"")
                    '\\' -> append("\\\\")
                    '\n' -> append("\\n")
                    '\r' -> append("\\r")
                    '\t' -> append("\\t")
                    else -> if (c < ' ') append("\\u%04x".format(c.code)) else append(c)
                }
            }
            append('"')
        }
    }
}

/** Companion shim to [JSONObject]; same scope and caveats. */
class JSONArray {

    private val values = ArrayList<Any?>()

    constructor()
    constructor(source: Collection<*>) { values.addAll(source) }

    fun put(value: Any?): JSONArray { values.add(value); return this }
    fun put(value: Int): JSONArray = put(value as Any)
    fun put(value: Long): JSONArray = put(value as Any)
    fun put(value: Double): JSONArray = put(value as Any)
    fun put(value: Boolean): JSONArray = put(value as Any)

    fun get(index: Int): Any =
        values.getOrNull(index) ?: throw JSONException("index $index out of range")

    fun opt(index: Int): Any? = values.getOrNull(index)

    fun getString(index: Int): String = get(index).toString()
    fun getInt(index: Int): Int = (get(index) as Number).toInt()
    fun getDouble(index: Int): Double = (get(index) as Number).toDouble()
    fun getJSONObject(index: Int): JSONObject = get(index) as JSONObject
    fun optJSONObject(index: Int): JSONObject? = values.getOrNull(index) as? JSONObject

    fun length(): Int = values.size

    override fun toString(): String =
        values.joinToString(",", "[", "]") { JSONObject.render(it) }
}

/** Unchecked, matching the real org.json contract. */
class JSONException : RuntimeException {
    constructor(message: String) : super(message)
    constructor(message: String, cause: Throwable) : super(message, cause)
}
