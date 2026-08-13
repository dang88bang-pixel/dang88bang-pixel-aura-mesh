package com.aura.agent.fusion

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Binder
import android.os.Build
import android.os.IBinder
import android.util.Log
import com.aura.agent.security.CausalValidator
import com.aura.agent.security.Severity
import com.aura.agent.sensors.BleScanner
import com.aura.agent.sensors.ImuManager
import com.aura.agent.sensors.LidarManager
import com.aura.agent.sensors.MmwaveManager
import com.aura.agent.storage.LocalVectorStore
import com.aura.agent.storage.Transform3D
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import org.json.JSONObject

private const val TAG = "AuraFusion"
private const val CHANNEL_ID = "aura_fusion"
private const val NOTIFICATION_ID = 4711

/**
 * Foreground service running the fusion loop.
 *
 * A foreground service is mandatory here: surveys run for tens of minutes with
 * the screen off, and a plain background service would be frozen by Doze after
 * a few minutes, silently truncating the recording.
 *
 * Loop order matches `edge-agent/aura/fusion.py`:
 *   IMU predict -> UWB ranges -> LiDAR scan match -> BLE fix -> persist.
 */
class SensorFusionService : Service() {

    private val binder = LocalBinder()
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)

    private lateinit var ekf: NativeEkf
    private lateinit var store: LocalVectorStore
    private lateinit var imu: ImuManager
    private lateinit var ble: BleScanner
    private lateinit var audit: CausalValidator

    private var lidar: LidarManager? = null
    private var mmwave: MmwaveManager? = null

    private val _state = MutableStateFlow(FusionState())
    val state: StateFlow<FusionState> = _state.asStateFlow()

    private var iterations = 0L
    private var lastPredictAt = 0L
    private var lastPersistAt = 0L
    private var deviceHeight = 1.4f

    /** Surveyed UWB anchor positions; empty until the site is configured. */
    val uwbAnchors = mutableMapOf<String, FloatArray>()

    inner class LocalBinder : Binder() {
        fun service(): SensorFusionService = this@SensorFusionService
    }

    override fun onBind(intent: Intent?): IBinder = binder

    override fun onCreate() {
        super.onCreate()
        ekf = NativeEkf()
        store = LocalVectorStore(applicationContext)
        imu = ImuManager(applicationContext)
        ble = BleScanner(applicationContext)
        audit = CausalValidator()
        createNotificationChannel()
        audit.append("service", "fusion.create", severity = Severity.NOTICE)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        startForeground(NOTIFICATION_ID, buildNotification("Sensorfusion aktiv"))
        startPipeline()
        // START_STICKY: if the OS kills us under memory pressure mid-survey we
        // want to come back and keep recording rather than lose the session.
        return START_STICKY
    }

    private fun startPipeline() {
        imu.start()
        ble.start()
        lidar?.start()
        mmwave?.start()
        audit.append("service", "fusion.start", severity = Severity.SECURITY)

        // --- IMU prediction ------------------------------------------
        scope.launch {
            imu.samples.collect { sample ->
                val now = sample.timestamp
                val dt = if (lastPredictAt == 0L) 0.02f
                else ((now - lastPredictAt) / 1000.0f).coerceIn(0.001f, 0.5f)
                lastPredictAt = now

                ekf.predict(sample.gyro, sample.accel, dt)
                if (imu.isStatic) ekf.updateZeroVelocity()
                ekf.updateYaw(sample.heading(), sigma = 0.35f)
                // Weak height prior keeps the vertical channel observable when
                // no barometer fix is available.
                ekf.updateAltitude(deviceHeight, sigma = 0.45f)
                iterations++
            }
        }

        // --- LiDAR scan matching --------------------------------------
        scope.launch {
            lidar?.scans?.collect { scan ->
                val snapshot = ekf.snapshot()
                // A full scan matcher runs natively; until a map exists the
                // scan is only used for mapping, never for a pose update.
                _state.value = _state.value.copy(
                    lidarPoints = scan.size,
                    lastScanAt = scan.timestamp,
                )
            }
        }

        // --- BLE multilateration --------------------------------------
        scope.launch {
            while (isActive) {
                ble.multilaterate()?.let { (x, y, sigma) ->
                    val position = ekf.position()
                    val offset = kotlin.math.hypot(x - position[0], y - position[1])
                    // Gate the fix: RSSI trilateration occasionally produces
                    // wild outliers that would yank the filter across the room.
                    if (offset <= 3.0f * maxOf(1.0f, sigma)) {
                        ekf.updatePosition(floatArrayOf(x, y, position[2]), maxOf(1.5f, sigma))
                    }
                }
                delay(500)
            }
        }

        // --- persistence and telemetry --------------------------------
        scope.launch {
            while (isActive) {
                val snapshot = ekf.snapshot()
                _state.value = _state.value.copy(
                    ekf = snapshot,
                    iterations = iterations,
                    beacons = ble.beacons.value.size,
                    running = true,
                )
                val now = System.currentTimeMillis()
                if (now - lastPersistAt > 500) {
                    lastPersistAt = now
                    store.saveTransform(
                        Transform3D(
                            snapshot.position[0], snapshot.position[1], snapshot.position[2],
                            snapshot.attitude[0], snapshot.attitude[1], snapshot.attitude[2],
                        ),
                        covariance = ekf.covarianceDiagonal().copyOfRange(0, 9),
                        velocity = snapshot.velocity,
                        metadata = JSONObject().apply {
                            put("iteration", iterations)
                            put("converged", snapshot.converged)
                            put("beacons", ble.beacons.value.size)
                        },
                    )
                }
                delay(100)
            }
        }

        // --- retention -------------------------------------------------
        scope.launch {
            while (isActive) {
                delay(60_000)
                val removed = store.enforceRetention(7 * 24 * 3600, 250_000)
                if (removed.transforms > 0 || removed.events > 0) {
                    Log.i(TAG, "retention removed ${removed.transforms} transforms, ${removed.events} events")
                }
            }
        }
    }

    fun attachLidar(manager: LidarManager) { lidar = manager }

    fun attachMmwave(manager: MmwaveManager) { mmwave = manager }

    fun setDeviceHeight(height: Float) { deviceHeight = height }

    fun addUwbAnchor(id: String, x: Float, y: Float, z: Float) {
        uwbAnchors[id] = floatArrayOf(x, y, z)
        audit.append("config", "uwb.anchor.add", JSONObject().apply {
            put("id", id); put("x", x); put("y", y); put("z", z)
        }, Severity.NOTICE)
    }

    /** Feed a UWB two-way-ranging result into the filter. */
    fun onUwbRange(anchorId: String, distance: Float, lineOfSight: Boolean) {
        val anchor = uwbAnchors[anchorId] ?: return
        // Non-line-of-sight links are still informative; inflating sigma beats
        // discarding them, because 1-2 anchors alone leave the solution free
        // to slide along the unobservable direction.
        ekf.updateUwbRange(anchor, distance, if (lineOfSight) 0.12f else 0.55f)
    }

    fun auditChain(): CausalValidator = audit

    fun verifyAuditChain(): Boolean = audit.verify().valid

    override fun onDestroy() {
        audit.append("service", "fusion.stop", severity = Severity.SECURITY)
        imu.stop()
        ble.stop()
        lidar?.stop()
        mmwave?.stop()
        scope.cancel()
        ekf.close()
        store.close()
        super.onDestroy()
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                "Aura Sensorfusion",
                NotificationManager.IMPORTANCE_LOW,
            ).apply { description = "Laufende Sensorerfassung und 3D-Kartierung" }
            (getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager)
                .createNotificationChannel(channel)
        }
    }

    private fun buildNotification(text: String): Notification =
        Notification.Builder(this, CHANNEL_ID)
            .setContentTitle("AURA 6.0")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.ic_menu_compass)
            .setOngoing(true)
            .build()
}

data class FusionState(
    val ekf: EkfSnapshot? = null,
    val iterations: Long = 0,
    val beacons: Int = 0,
    val lidarPoints: Int = 0,
    val lastScanAt: Long = 0,
    val running: Boolean = false,
)
