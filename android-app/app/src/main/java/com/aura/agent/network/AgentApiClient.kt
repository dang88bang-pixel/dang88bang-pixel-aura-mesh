package com.aura.agent.network

import android.util.Log
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import java.util.concurrent.TimeUnit
import kotlin.math.min
import kotlin.math.pow

private const val TAG = "AuraApi"

/**
 * REST + WebSocket client for the edge agent.
 *
 * The WebSocket reconnects with exponential backoff and jitter. That detail
 * matters in the field: when a squad of CT45Ps loses the mesh AP they all
 * reconnect at once, and a fixed retry interval turns into a synchronised
 * thundering herd that keeps the agent from ever coming back up.
 */
class AgentApiClient(
    baseUrl: String = "http://10.8.0.1:8080",
    private val apiToken: String = "",
    private val scope: CoroutineScope = CoroutineScope(Dispatchers.IO),
) {
    var baseUrl: String = baseUrl.trimEnd('/')
        set(value) { field = value.trimEnd('/') }

    private val client = OkHttpClient.Builder()
        .connectTimeout(5, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .writeTimeout(10, TimeUnit.SECONDS)
        // Keep the socket warm; the agent sends telemetry at 10 Hz.
        .pingInterval(20, TimeUnit.SECONDS)
        .retryOnConnectionFailure(true)
        .build()

    private var webSocket: WebSocket? = null
    private var reconnectAttempt = 0
    private var shouldReconnect = true

    private val _telemetry = MutableSharedFlow<JSONObject>(replay = 1, extraBufferCapacity = 8)
    val telemetry: SharedFlow<JSONObject> = _telemetry.asSharedFlow()

    private val _connection = MutableStateFlow(ConnectionState.DISCONNECTED)
    val connection: StateFlow<ConnectionState> = _connection.asStateFlow()

    // ------------------------------------------------------------------
    // REST
    // ------------------------------------------------------------------
    suspend fun getState(): JSONObject? = get("/api/v1/agent/state")

    suspend fun getHistory(limit: Int = 500): JSONObject? = get("/api/v1/agent/history?limit=$limit")

    suspend fun getMap(maxPoints: Int = 4000): JSONObject? = get("/api/v1/agent/map?max_points=$maxPoints")

    suspend fun startScenario(
        scenario: String,
        people: Int,
        smokeDensity: Float,
        panic: Float = 0.3f,
        duration: Float = 180f,
    ): JSONObject? = post("/api/v1/agent/scenario/start", JSONObject().apply {
        put("scenario", scenario)
        put("people", people)
        put("smoke_density", smokeDensity)
        put("panic", panic)
        put("duration", duration)
    })

    suspend fun stopScenario(): JSONObject? = post("/api/v1/agent/scenario/stop", JSONObject())

    suspend fun setConfig(patch: JSONObject): JSONObject? = post("/api/v1/agent/config", patch)

    /** Upload this device's observations so the agent can fuse the mesh. */
    suspend fun ingest(deviceId: String, rssi: Map<String, Int>, position: FloatArray?, battery: Float) =
        post("/api/v1/agent/ingest", JSONObject().apply {
            put("device_id", deviceId)
            put("timestamp", System.currentTimeMillis() / 1000.0)
            put("rssi", JSONObject(rssi.mapValues { it.value }))
            position?.let { put("position", org.json.JSONArray(listOf(it[0], it[1], it[2]))) }
            put("battery", battery)
        })

    private suspend fun get(path: String): JSONObject? = execute(
        Request.Builder().url("$baseUrl$path").get().applyAuth().build()
    )

    private suspend fun post(path: String, body: JSONObject): JSONObject? = execute(
        Request.Builder()
            .url("$baseUrl$path")
            .post(body.toString().toRequestBody(JSON_MEDIA_TYPE))
            .applyAuth()
            .build()
    )

    private fun Request.Builder.applyAuth(): Request.Builder =
        if (apiToken.isNotBlank()) header("Authorization", "Bearer $apiToken") else this

    private suspend fun execute(request: Request): JSONObject? = withIo {
        runCatching {
            client.newCall(request).execute().use { response: Response ->
                if (!response.isSuccessful) {
                    Log.w(TAG, "${request.url} -> HTTP ${response.code}")
                    return@use null
                }
                response.body?.string()?.let { JSONObject(it) }
            }
        }.getOrElse {
            Log.w(TAG, "request failed: ${it.message}")
            null
        }
    }

    private suspend fun <T> withIo(block: () -> T): T =
        kotlinx.coroutines.withContext(Dispatchers.IO) { block() }

    // ------------------------------------------------------------------
    // WebSocket
    // ------------------------------------------------------------------
    fun connect() {
        shouldReconnect = true
        openSocket()
    }

    private fun openSocket() {
        val wsUrl = baseUrl.replaceFirst("http://", "ws://").replaceFirst("https://", "wss://") +
            "/ws/agent/events" + if (apiToken.isNotBlank()) "?token=$apiToken" else ""

        _connection.value = ConnectionState.CONNECTING
        val request = Request.Builder().url(wsUrl).build()

        webSocket = client.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                Log.i(TAG, "websocket connected to $wsUrl")
                reconnectAttempt = 0
                _connection.value = ConnectionState.CONNECTED
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                runCatching { JSONObject(text) }
                    .onSuccess { _telemetry.tryEmit(it) }
                    .onFailure { Log.w(TAG, "malformed frame: ${it.message}") }
            }

            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                Log.i(TAG, "websocket closing: $code $reason")
                webSocket.close(1000, null)
                _connection.value = ConnectionState.DISCONNECTED
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                Log.w(TAG, "websocket failure: ${t.message}")
                _connection.value = ConnectionState.DISCONNECTED
                scheduleReconnect()
            }
        })
    }

    private fun scheduleReconnect() {
        if (!shouldReconnect) return
        scope.launch {
            // Exponential backoff capped at 30 s, plus jitter so a whole squad
            // of devices does not retry in lockstep after an AP outage.
            val base = min(30_000.0, 500.0 * 2.0.pow(reconnectAttempt.toDouble()))
            val jitter = (Math.random() * base * 0.3).toLong()
            reconnectAttempt = (reconnectAttempt + 1).coerceAtMost(6)
            _connection.value = ConnectionState.RECONNECTING
            delay(base.toLong() + jitter)
            if (shouldReconnect) openSocket()
        }
    }

    fun send(command: String, payload: JSONObject = JSONObject()): Boolean {
        val message = JSONObject().apply {
            put("command", command)
            put("payload", payload)
        }
        return webSocket?.send(message.toString()) ?: false
    }

    fun disconnect() {
        shouldReconnect = false
        webSocket?.close(1000, "client shutdown")
        webSocket = null
        _connection.value = ConnectionState.DISCONNECTED
    }

    companion object {
        private val JSON_MEDIA_TYPE = "application/json; charset=utf-8".toMediaType()
    }
}

enum class ConnectionState { DISCONNECTED, CONNECTING, CONNECTED, RECONNECTING }
