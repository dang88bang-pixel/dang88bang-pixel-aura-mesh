package com.aura.agent

import android.app.Application
import android.util.Log
import com.aura.agent.security.CausalValidator
import com.aura.agent.security.Severity

/**
 * Application singleton: owns the process-wide audit chain and the native
 * engine handle so they survive activity recreation (rotation, theme change).
 */
class AuraApplication : Application() {

    lateinit var audit: CausalValidator
        private set

    override fun onCreate() {
        super.onCreate()
        instance = this
        audit = CausalValidator()
        audit.append("app", "application.start", severity = Severity.NOTICE)
        Log.i("Aura", "AURA 6.0 starting, native core: ${NativeEngine.version()}")
    }

    companion object {
        lateinit var instance: AuraApplication
            private set
    }
}

/** Metadata about the loaded native library. */
object NativeEngine {
    fun version(): String = runCatching { nativeVersion() }.getOrDefault("unavailable")
    fun hasNeon(): Boolean = runCatching { nativeHasNeon() }.getOrDefault(false)

    private external fun nativeVersion(): String
    private external fun nativeHasNeon(): Boolean

    init {
        runCatching { System.loadLibrary("aura_core") }
    }
}
