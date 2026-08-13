package com.aura.agent

import android.app.Application
import android.util.Log
import com.aura.agent.network.AgentApiClient
import com.aura.agent.security.CausalValidator
import com.aura.agent.security.Severity

/**
 * Application singleton: owns the process-wide audit chain and the native
 * engine handle so they survive activity recreation (rotation, theme change).
 */
class AuraApplication : Application() {

    lateinit var audit: CausalValidator
        private set

    /**
     * Shared client for the edge agent.
     *
     * One instance for the whole process: it owns an OkHttp connection pool
     * and a WebSocket, and creating one per fragment would open a new socket
     * on every tab swipe.
     */
    lateinit var api: AgentApiClient
        private set

    /** Persisted agent URL, shared with the WebView in MapFragment. */
    var agentUrl: String
        get() = prefs().getString(KEY_AGENT_URL, DEFAULT_AGENT_URL) ?: DEFAULT_AGENT_URL
        set(value) {
            prefs().edit().putString(KEY_AGENT_URL, value).apply()
            api.baseUrl = value
        }

    /**
     * Run the mmWave sensor at reduced transmit power.
     *
     * Halves range but materially extends battery life on a long survey. Read
     * by MmwaveManager when it configures the sensor.
     */
    var reducedMmwavePower: Boolean
        get() = prefs().getBoolean(KEY_MMWAVE_REDUCED, false)
        set(value) = prefs().edit().putBoolean(KEY_MMWAVE_REDUCED, value).apply()

    private fun prefs() = getSharedPreferences(PREFS, MODE_PRIVATE)

    override fun onCreate() {
        super.onCreate()
        instance = this
        audit = CausalValidator()
        api = AgentApiClient(baseUrl = agentUrl)
        audit.append("app", "application.start", severity = Severity.NOTICE)
        Log.i("Aura", "AURA 6.0 starting, native core: ${NativeEngine.version()}")
    }

    companion object {
        lateinit var instance: AuraApplication
            private set

        /** Same file and key MapFragment reads, so both agree on the URL. */
        const val PREFS = "aura"
        const val KEY_AGENT_URL = "agent_url"
        const val KEY_MMWAVE_REDUCED = "mmwave_reduced_power"
        const val DEFAULT_AGENT_URL = "http://10.8.0.1:8080"
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
