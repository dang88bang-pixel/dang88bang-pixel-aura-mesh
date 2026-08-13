package com.aura.agent.fusion

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.os.Binder
import android.os.Build
import android.os.IBinder
import android.util.Log
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat

import com.aura.agent.security.CausalValidator
import com.aura.agent.security.Severity
import com.aura.agent.sensors.BleBeacon
import com.aura.agent.sensors.BleScanner
import com.aura.agent.sensors.ImuManager
import com.aura.agent.sensors.LidarManager
import com.aura.agent.sensors.LidarScan
import com.aura.agent.sensors.MmwaveManager
import com.aura.agent.sensors.UsbSerialTransport
import com.aura.agent.sensors.UwbManager
import com.aura.agent.sensors.UwbReading
import com.aura.agent.sensors.VitalsEstimator
import com.aura.agent.rti.NativeRti
import com.aura.agent.rti.RtiNode
import com.aura.agent.rti.RtiTarget
import com.aura.agent.storage.LocalVectorStore
import com.aura.agent.storage.Transform3D
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
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

/** RTI grid margin, cadence and size ceiling. */
private const val RTI_MARGIN_M = 1.0f
private const val RTI_INTERVAL_MS = 500L
private const val RTI_MAX_VOXELS = 20000
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
     * Radio-tomographic imaging over the BLE token mesh, when one is surveyed.
     *
     * RTI needs the node positions to be known: it solves for attenuation on
     * each link, so a node whose location is wrong corrupts every link it
     * takes part in. It stays null until [configureRti] is called with a
     * surveyed layout — guessing positions from RSSI and then imaging with
     * them would produce a confident picture of nothing.
     */
    private var rti: NativeRti? = null
    private val _rtiTargets = MutableStateFlow<List<RtiTarget>>(emptyList())
    val rtiTargets: StateFlow<List<RtiTarget>> = _rtiTargets.asStateFlow()
    private var lastRtiAt = 0L

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
    /**
     * Enable RTI over a surveyed set of BLE nodes.
     *
     * @param nodes surveyed positions, at least 3
     * @param resolution voxel size in metres
     */
    fun configureRti(nodes: List<RtiNode>, resolution: Float = 0.25f): Boolean {
        if (nodes.size < 3) {
            Log.w(TAG, "RTI needs at least 3 nodes, got ${'$'}{nodes.size}")
            return false
        }
        rti?.close()
        val minX = nodes.minOf { it.x } - RTI_MARGIN_M
        val minY = nodes.minOf { it.y } - RTI_MARGIN_M
        val spanX = nodes.maxOf { it.x } + RTI_MARGIN_M - minX
        val spanY = nodes.maxOf { it.y } + RTI_MARGIN_M - minY
        val nx = kotlin.math.ceil(spanX / resolution).toInt().coerceAtLeast(1)
        val ny = kotlin.math.ceil(spanY / resolution).toInt().coerceAtLeast(1)
        if (nx * ny > RTI_MAX_VOXELS) {
            // The solver is O(links x voxels) per FISTA iteration; an
            // unbounded grid stalls the fusion loop rather than failing.
            Log.w(TAG, "RTI grid ${'$'}nx x ${'$'}ny exceeds ${'$'}RTI_MAX_VOXELS voxels")
            return false
        }
        rti = NativeRti(nodes, minX, minY, resolution, nx, ny)
        audit.append(
            "rti", "configured",
            JSONObject().put("nodes", nodes.size).put("voxels", nx * ny),
            Severity.NOTICE,
        )
        return true
    }

    /** Feed reciprocal link RSSI; calibrates first, then images. */
    fun onRtiSample(linkRssi: Map<Pair<String, String>, Float>) {
        val engine = rti ?: return
        val now = System.currentTimeMillis()
        if (now - lastRtiAt < RTI_INTERVAL_MS) return
        lastRtiAt = now

        if (!engine.isCalibrated) {
            engine.calibrate(linkRssi)
            return
        }
        val image = engine.reconstruct(linkRssi) ?: return
        _rtiTargets.value = engine.extractTargets(image)
    }

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
        if (!enterForeground()) {
            // Without the runtime permissions behind the declared service
            // types, Android 14 throws instead of starting us. Stopping
            // cleanly and saying why beats being killed mid-survey with a
            // SecurityException the operator never sees.
            audit.append(
                "service", "fusion.foreground_denied",
                JSONObject().put("reason", "missing runtime permission"),
                Severity.WARNING,
            )
            stopSelf()
            return START_NOT_STICKY
        }
        startPipeline()
        // START_STICKY: if the OS kills us under memory pressure mid-survey we
        // want to come back and keep recording rather than lose the session.
        return START_STICKY
    }

    /**
     * Enter the foreground with only the service types we may legally claim.
     *
     * On API 34 the declared `foregroundServiceType` is enforced at
     * `startForeground` time: claiming `location` without ACCESS_FINE_LOCATION
     * granted is an immediate SecurityException, not a downgrade. The type is
     * therefore assembled from what has actually been granted, so a survey
     * with BLE but no location permission still runs.
     */
    private fun enterForeground(): Boolean {
        val notification = buildNotification("Sensorfusion aktiv")
        return try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                var types = 0
                if (hasPermission(Manifest.permission.ACCESS_FINE_LOCATION)) {
                    types = types or ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION
                }
                val btOk = Build.VERSION.SDK_INT < Build.VERSION_CODES.S ||
                    hasPermission(Manifest.permission.BLUETOOTH_CONNECT)
                if (btOk) {
                    types = types or ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE
                }
                if (types == 0) return false
                ServiceCompat.startForeground(this, NOTIFICATION_ID, notification, types)
            } else {
                startForeground(NOTIFICATION_ID, notification)
            }
            true
        } catch (e: SecurityException) {
            Log.e(TAG, "startForeground rejected: ${'$'}{e.message}")
            false
        } catch (e: IllegalStateException) {
            // Thrown when started from the background outside an allowed slot.
            Log.e(TAG, "startForeground not permitted right now: ${'$'}{e.message}")
            false
        }
    }

    private fun hasPermission(name: String): Boolean =
        ContextCompat.checkSelfPermission(this, name) == PackageManager.PERMISSION_GRANTED

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
        rti?.close()
        rti = null
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
