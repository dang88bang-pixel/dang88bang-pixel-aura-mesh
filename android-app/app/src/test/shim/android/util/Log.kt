@file:JvmName("LogShim")

package android.util

/**
 * Host-side stand-in for `android.util.Log`.
 *
 * On a JVM the real class is absent (or throws "not mocked"). Logging is the
 * only Android dependency in the otherwise pure-logic sensor parsers, so this
 * tiny shim lets them be compiled and tested without Robolectric.
 *
 * Test scope only.
 */
object Log {
    var verbose = false

    @JvmStatic fun v(tag: String, msg: String): Int = emit("V", tag, msg)
    @JvmStatic fun d(tag: String, msg: String): Int = emit("D", tag, msg)
    @JvmStatic fun i(tag: String, msg: String): Int = emit("I", tag, msg)
    @JvmStatic fun w(tag: String, msg: String): Int = emit("W", tag, msg)
    @JvmStatic fun w(tag: String, msg: String, tr: Throwable): Int = emit("W", tag, "$msg: $tr")
    @JvmStatic fun e(tag: String, msg: String): Int = emit("E", tag, msg)
    @JvmStatic fun e(tag: String, msg: String, tr: Throwable): Int = emit("E", tag, "$msg: $tr")

    private fun emit(level: String, tag: String, msg: String): Int {
        if (verbose) println("$level/$tag: $msg")
        return 0
    }
}
