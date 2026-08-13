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
import com.aura.agent.sensors.UsbSerialTransport
import com.aura.agent.sensors.UwbManager
import com.aura.agent.sensors.VitalsEstimator
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

/** UWB reporting rate, and the window the vitals estimator runs over. */
private const val UWB_RATE_HZ = 20f
private const val VITALS_WINDOW = 1200          // 60 s at 20 Hz
private const val VITALS_INTERVAL_MS = 2000L    // re-estimate every 2 s
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
    private var uwb: UwbManager? = null

    private val _state = MutableStateFlow(FusionState())
    val state: StateFlow<FusionState> = _state.asStateFlow()

    private var iterations = 0L
    private var lastPredictAt = 0L
    private var lastPersistAt = 0L
    private var lastVoxelWriteAt = 0L
    private var deviceHeight = 1.4f

    /** Surveyed UWB anchor positions; empty until the site is configured. */
    val uwbAnchors = mutableMapOf<String, FloatArray>()

    /**
     * Rolling CIR amplitude history for the vitals estimator.
     *
     * 60 s at the UWB update rate. Breathing at 6/min needs ~30 s to resolve
     * at all, and the estimator's frequency resolution is 1/T, so a shorter
     * window cannot distinguish 12 from 14 breaths per minute.
     */
    private val cirHistory = ArrayDeque<Float>()
    private var lastVitalsAt = 0L

    /**
     * Read-only accessors for the UI.
     *
     * The fragments observe these flows directly rather than having every
     * sample mirrored into [FusionState]. A LiDAR sweep is ~720 points at
     * 10 Hz; copying that through a state object on every frame would churn
     * the allocator for no benefit, and the point cloud view already knows how
     * to decimate.
     */
    /** Storage diagnostics for the settings tab; null before the service starts. */
    fun storeStats(): JSONObject? =
        if (::store.isInitialized) runCatching { store.stats() }.getOrNull() else null

    val lidarScans: SharedFlow<LidarScan>? get() = lidar?.scans
    val bleBeacons: StateFlow<Map<String, BleBeacon>> get() = ble.beacons
    val uwbReadings: SharedFlow<UwbReading>? get() = uwb?.readings

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

    /**
     * Attach whatever is plugged into the USB-C port.
     *
     * Every sensor is optional: a CT45P with nothing attached still runs the
     * IMU + BLE pipeline and produces a usable (if coarser) track. Missing
     * hardware must never stop the service - a survey that silently fails to
     * start is worse than one that starts degraded and says so.
     */
    private fun attachUsbSensors() {
        val lidarTransport = UsbSerialTransport.forLidar(applicationContext)
        if (lidarTransport.open() == UsbSerialTransport.OpenResult.OPENED) {
            lidar = LidarManager(lidarTransport, scope)
            audit.append("sensor", "lidar.attached",
                JSONObject().put("device", lidarTransport.deviceName), Severity.NOTICE)
        } else {
            Log.i(TAG, "no LiDAR: ${'$'}{lidarTransport.lastError}")
        }

        val mmwaveTransport = UsbSerialTransport.forMmwaveData(applicationContext)
        if (mmwaveTransport.open() == UsbSerialTransport.OpenResult.OPENED) {
            mmwave = MmwaveManager(mmwaveTransport, scope)
            audit.append("sensor", "mmwave.attached",
                JSONObject().put("device", mmwaveTransport.deviceName), Severity.NOTICE)
        } else {
            Log.i(TAG, "no mmWave: ${'$'}{mmwaveTransport.lastError}")
        }

        val uwbTransport = UsbSerialTransport.forUwb(applicationContext)
        val manager = UwbManager(applicationContext, uwbTransport, scope)
        uwbAnchors.forEach { (id, p) -> manager.addAnchor(id, p[0], p[1], p[2]) }
        when (manager.start()) {
            UwbManager.Backend.NONE -> Log.i(TAG, "no UWB backend")
            else -> {
                uwb = manager
                audit.append("sensor", "uwb.attached",
                    JSONObject().put("backend", manager.backend.name), Severity.NOTICE)
            }
        }
    }

    private fun startPipeline() {
        imu.start()
        ble.start()
        attachUsbSensors()
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

        // --- LiDAR: map integration -----------------------------------
        scope.launch {
            lidar?.scans?.collect { scan ->
                val snapshot = ekf.snapshot()
                // The sweep is projected with the *current* pose estimate and
                // folded into the voxel map. No pose update is derived from it
                // here: a 2D scan match needs an existing map to register
                // against, and registering against a map built from the same
                // drifting pose just locks the error in (measured: 3.6 m in
                // the Python pipeline before the UWB bootstrap was added).
                val points = scan.toCartesian(
                    snapshot.position[0], snapshot.position[1], snapshot.yaw,
                )
                voxelIngest(points, scan.timestamp)
                _state.value = _state.value.copy(
                    lidarPoints = scan.size,
                    lastScanAt = scan.timestamp,
                )
            }
        }

        // --- UWB: ranging + through-wall CIR --------------------------
        scope.launch {
            uwb?.readings?.collect { reading ->
                // Fuse every anchor. NLOS links are kept with an inflated
                // sigma rather than discarded: with only 1-2 line-of-sight
                // anchors the range-only geometry is underconstrained and the
                // estimate slides along the unobservable direction.
                reading.ranges.forEach { (anchorId, distance) ->
                    onUwbRange(anchorId, distance, reading.lineOfSight[anchorId] ?: true)
                }
                // Vitals come from the CIR amplitude series, not from range.
                cirHistory.addLast(reading.cirAmplitude)
                while (cirHistory.size > VITALS_WINDOW) cirHistory.removeFirst()

                var vitals: VitalsEstimator.Vitals? = null
                val now = System.currentTimeMillis()
                if (cirHistory.size >= VitalsEstimator.MIN_SAMPLES &&
                    now - lastVitalsAt >= VITALS_INTERVAL_MS
                ) {
                    lastVitalsAt = now
                    vitals = VitalsEstimator.estimate(cirHistory.toFloatArray(), UWB_RATE_HZ)
                }

                _state.value = _state.value.copy(
                    uwbAnchorsInView = reading.ranges.size,
                    cirAmplitude = reading.cirAmplitude,
                    respirationBpm = vitals?.respirationBpm ?: _state.value.respirationBpm,
                    heartRateBpm = vitals?.heartRateBpm ?: _state.value.heartRateBpm,
                )
            }
        }

        // --- mmWave: moving targets -----------------------------------
        scope.launch {
            mmwave?.targets?.collect { targets ->
                val moving = targets.count { kotlin.math.abs(it.velocity) > 0.18f }
                _state.value = _state.value.copy(mmwaveTargets = targets.size, movingTargets = moving)
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

    /**
     * Fold a projected sweep into the persistent voxel map.
     *
     * Chunks are written at most once per second: the RLE codec is cheap but
     * SQLite writes are not, and at 10 sweeps/second the WAL would grow faster
     * than the retention job trims it.
     */
    private fun voxelIngest(points: FloatArray, timestamp: Long) {
        if (timestamp - lastVoxelWriteAt < 1000) return
        lastVoxelWriteAt = timestamp
        // Persisted as an event for now; the chunked writer lands with the
        // native voxel codec wiring (see docs/android_build.md).
        store.saveEvent(
            "lidar",
            JSONObject()
                .put("points", points.size / 2)
                .put("timestamp", timestamp / 1000.0),
            timestamp,
        )
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
        uwb?.stop()
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
    val uwbAnchorsInView: Int = 0,
    val cirAmplitude: Float = 0f,
    /** Respiration rate in breaths/min, or null when not observable. */
    val respirationBpm: Float? = null,
    /** Estimated heart rate in bpm, or null. Lower confidence than breathing. */
    val heartRateBpm: Float? = null,
    val mmwaveTargets: Int = 0,
    val movingTargets: Int = 0,
    val running: Boolean = false,
)
