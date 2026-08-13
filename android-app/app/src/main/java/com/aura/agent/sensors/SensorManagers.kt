package com.aura.agent.sensors

import android.bluetooth.BluetoothManager
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanFilter
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
import android.content.Context
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.util.Log
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.math.abs
import kotlin.math.atan2
import kotlin.math.cos
import kotlin.math.hypot
import kotlin.math.sin
import kotlin.math.sqrt

private const val TAG = "AuraSensors"

// ======================================================================
// shared types
// ======================================================================

data class LidarScan(
    val timestamp: Long,
    val angles: FloatArray,       // rad, body frame
    val distances: FloatArray,    // metres
    val quality: IntArray,
) {
    val size: Int get() = angles.size

    /** Project into the map frame given a pose. Returns [x0, y0, x1, y1, ...]. */
    fun toCartesian(px: Float, py: Float, yaw: Float): FloatArray {
        val out = FloatArray(size * 2)
        for (i in 0 until size) {
            val a = angles[i] + yaw
            out[2 * i] = px + distances[i] * cos(a)
            out[2 * i + 1] = py + distances[i] * sin(a)
        }
        return out
    }

    override fun equals(other: Any?): Boolean =
        other is LidarScan && timestamp == other.timestamp && angles.contentEquals(other.angles)

    override fun hashCode(): Int = 31 * timestamp.hashCode() + angles.contentHashCode()
}

data class MmwaveTarget(
    val x: Float, val y: Float, val z: Float,
    val velocity: Float, val snr: Float, val trackId: Int = -1,
) {
    val range: Float get() = sqrt(x * x + y * y + z * z)
    val azimuth: Float get() = atan2(y, x)
}

data class UwbReading(
    val timestamp: Long,
    val ranges: Map<String, Float>,
    val cirAmplitude: Float,
    val cirPhase: Float,
    val lineOfSight: Map<String, Boolean>,
)

data class BleBeacon(
    val address: String,
    val rssi: Int,
    val name: String,
    val timestamp: Long,
    val txPower: Int = -59,
) {
    /** Log-distance path loss, n = 2.4 for an indoor office. */
    val distance: Double get() = Math.pow(10.0, (txPower - rssi) / (10.0 * 2.4))
}

data class ImuSample(
    val timestamp: Long,
    val gyro: FloatArray,
    val accel: FloatArray,
    val mag: FloatArray,
) {
    fun heading(): Float = atan2(-mag[1], mag[0])

    override fun equals(other: Any?): Boolean =
        other is ImuSample && timestamp == other.timestamp && gyro.contentEquals(other.gyro)

    override fun hashCode(): Int = 31 * timestamp.hashCode() + gyro.contentHashCode()
}

/** Health of one driver, surfaced in the Settings screen. */
data class SensorHealth(
    val name: String,
    val connected: Boolean = false,
    val samples: Long = 0,
    val errors: Long = 0,
    val lastSampleAt: Long = 0,
    val detail: String = "",
) {
    val healthy: Boolean
        get() = connected && (lastSampleAt == 0L || System.currentTimeMillis() - lastSampleAt < 5000)
}

// ======================================================================
// USB serial transport
// ======================================================================

/**
 * Thin wrapper around a USB-serial device.
 *
 * The CT45P exposes USB-C host mode, which is how the LiDAR, the mmWave
 * front-end and the SDR attach. Kept abstract so tests can inject a fake
 * without hardware; the real implementation is [UsbSerialTransport].
 *
 * `read` returns the byte count, **0 on timeout** (a normal idle state, not an
 * error) and -1 on failure.
 */
interface SerialTransport {
    val isOpen: Boolean
    fun write(bytes: ByteArray)
    fun read(buffer: ByteArray, timeoutMs: Int): Int
    fun close()
}

// ======================================================================
// LiDAR (RPLIDAR A1/A2/S2)
// ======================================================================

class LidarManager(
    private val transport: SerialTransport?,
    private val scope: CoroutineScope,
) {
    private val scanning = AtomicBoolean(false)
    private val buffer = ArrayDeque<Byte>()
    private var job: Job? = null

    private val _scans = MutableSharedFlow<LidarScan>(replay = 1, extraBufferCapacity = 4)
    val scans: SharedFlow<LidarScan> = _scans.asSharedFlow()

    private val _health = MutableStateFlow(SensorHealth("lidar"))
    val health: StateFlow<SensorHealth> = _health.asStateFlow()

    var maxRange = 16.0f

    fun start() {
        if (!scanning.compareAndSet(false, true)) return
        transport?.write(byteArrayOf(SYNC.toByte(), CMD_STOP.toByte()))
        Thread.sleep(50)
        transport?.write(byteArrayOf(SYNC.toByte(), CMD_RESET.toByte()))
        Thread.sleep(800)
        startExpressScan()
        _health.value = _health.value.copy(connected = true, detail = "EXPRESS_SCAN")

        job = scope.launch(Dispatchers.IO) {
            val chunk = ByteArray(4096)
            while (isActive && scanning.get()) {
                val read = transport?.read(chunk, 200) ?: -1
                if (read > 0) {
                    for (i in 0 until read) buffer.addLast(chunk[i])
                    parseBuffer()?.let { scan ->
                        _scans.tryEmit(scan)
                        _health.value = _health.value.copy(
                            samples = _health.value.samples + 1,
                            lastSampleAt = System.currentTimeMillis(),
                        )
                    }
                }
            }
        }
    }

    private fun startExpressScan() {
        val payload = byteArrayOf(0x05, 0x00, 0x00, 0x00, 0x00)
        val frame = byteArrayOf(SYNC.toByte(), CMD_EXPRESS_SCAN.toByte(), payload.size.toByte()) + payload
        var checksum = 0
        frame.forEach { checksum = checksum xor it.toInt() }
        transport?.write(frame + byteArrayOf(checksum.toByte()))
    }

    /**
     * Decode legacy 5-byte measurement nodes.
     *
     * Each node carries a start flag S and its inverse; when they are equal
     * the stream is misaligned and we must resync byte by byte rather than
     * blindly consuming 5 bytes, otherwise the scan is garbage forever.
     */
    private fun parseBuffer(): LidarScan? {
        val angles = ArrayList<Float>()
        val distances = ArrayList<Float>()
        val quality = ArrayList<Int>()

        while (buffer.size >= 5) {
            val b0 = buffer.first().toInt() and 0xFF
            val start = b0 and 0x01
            val inverted = (b0 shr 1) and 0x01
            if (start == inverted) {
                buffer.removeFirst()
                continue
            }
            val node = ByteArray(5)
            val iterator = buffer.iterator()
            for (i in 0 until 5) node[i] = iterator.next()
            if ((node[1].toInt() and 0x01) == 0) {
                buffer.removeFirst()
                continue
            }
            repeat(5) { buffer.removeFirst() }

            val q = (node[0].toInt() and 0xFF) shr 2
            val angleQ6 = (((node[2].toInt() and 0xFF) shl 7) or ((node[1].toInt() and 0xFF) shr 1)) and 0x7FFF
            val distQ2 = ((node[4].toInt() and 0xFF) shl 8) or (node[3].toInt() and 0xFF)
            if (distQ2 == 0) continue

            val distance = distQ2 / 4000.0f
            if (distance > maxRange) continue
            angles += Math.toRadians(angleQ6 / 64.0).toFloat() - Math.PI.toFloat()
            distances += distance
            quality += q
        }
        if (angles.isEmpty()) return null
        return LidarScan(
            System.currentTimeMillis(),
            angles.toFloatArray(),
            distances.toFloatArray(),
            quality.toIntArray(),
        )
    }

    fun stop() {
        scanning.set(false)
        job?.cancel()
        transport?.write(byteArrayOf(SYNC.toByte(), CMD_STOP.toByte()))
        _health.value = _health.value.copy(connected = false)
    }

    companion object {
        private const val SYNC = 0xA5
        private const val CMD_STOP = 0x25
        private const val CMD_RESET = 0x40
        private const val CMD_EXPRESS_SCAN = 0x82
    }
}

// ======================================================================
// mmWave (TI IWR6843)
// ======================================================================

class MmwaveManager(
    private val transport: SerialTransport?,
    private val scope: CoroutineScope,
) {
    private val buffer = ArrayList<Byte>(16384)
    private var job: Job? = null
    private val running = AtomicBoolean(false)

    private val _targets = MutableSharedFlow<List<MmwaveTarget>>(replay = 1, extraBufferCapacity = 4)
    val targets: SharedFlow<List<MmwaveTarget>> = _targets.asSharedFlow()

    private val _health = MutableStateFlow(SensorHealth("mmwave"))
    val health: StateFlow<SensorHealth> = _health.asStateFlow()

    var reducedProfile = false
        private set

    fun configureProfile(reduced: Boolean) {
        reducedProfile = reduced
        val config = if (reduced) MMWAVE_REDUCED else MMWAVE_FULL
        config.lines().filter { it.isNotBlank() }.forEach { line ->
            transport?.write((line + "\n").toByteArray())
            Thread.sleep(10)
        }
        _health.value = _health.value.copy(detail = if (reduced) "reduced" else "full")
    }

    fun start() {
        if (!running.compareAndSet(false, true)) return
        configureProfile(reducedProfile)
        _health.value = _health.value.copy(connected = true)
        job = scope.launch(Dispatchers.IO) {
            val chunk = ByteArray(8192)
            while (isActive && running.get()) {
                val read = transport?.read(chunk, 100) ?: -1
                if (read > 0) {
                    for (i in 0 until read) buffer.add(chunk[i])
                    val parsed = parseFrames()
                    if (parsed.isNotEmpty()) {
                        _targets.tryEmit(parsed)
                        _health.value = _health.value.copy(
                            samples = _health.value.samples + 1,
                            lastSampleAt = System.currentTimeMillis(),
                        )
                    }
                }
            }
        }
    }

    /** Parse the TLV stream: magic word, 32-byte header, then typed blocks. */
    private fun parseFrames(): List<MmwaveTarget> {
        val out = mutableListOf<MmwaveTarget>()
        while (true) {
            val start = indexOfMagic() ?: break
            if (start > 0) repeat(start) { buffer.removeAt(0) }
            if (buffer.size < 40) break

            val totalLength = readUInt32(16)
            val numTlvs = readUInt32(32)
            if (totalLength < 40 || totalLength > (1 shl 20)) {
                repeat(8) { if (buffer.isNotEmpty()) buffer.removeAt(0) }
                continue
            }
            if (buffer.size < totalLength) break

            var offset = 40
            repeat(numTlvs) {
                if (offset + 8 > totalLength) return@repeat
                val tlvType = readUInt32(offset)
                val tlvLength = readUInt32(offset + 4)
                if (tlvType == TLV_DETECTED_POINTS) {
                    val count = (tlvLength - 8) / 16
                    for (p in 0 until count) {
                        val base = offset + 8 + p * 16
                        if (base + 16 > totalLength) break
                        out += MmwaveTarget(
                            x = readFloat(base),
                            y = readFloat(base + 4),
                            z = readFloat(base + 8),
                            velocity = readFloat(base + 12),
                            snr = 0f,
                        )
                    }
                }
                offset += tlvLength
            }
            repeat(totalLength) { if (buffer.isNotEmpty()) buffer.removeAt(0) }
        }
        return out
    }

    private fun indexOfMagic(): Int? {
        outer@ for (i in 0..buffer.size - MAGIC.size) {
            for (j in MAGIC.indices) {
                if (buffer[i + j] != MAGIC[j]) continue@outer
            }
            return i
        }
        return null
    }

    private fun readUInt32(offset: Int): Int =
        (buffer[offset].toInt() and 0xFF) or
            ((buffer[offset + 1].toInt() and 0xFF) shl 8) or
            ((buffer[offset + 2].toInt() and 0xFF) shl 16) or
            ((buffer[offset + 3].toInt() and 0xFF) shl 24)

    private fun readFloat(offset: Int): Float = Float.fromBits(readUInt32(offset))

    fun stop() {
        running.set(false)
        job?.cancel()
        transport?.write("sensorStop\n".toByteArray())
        _health.value = _health.value.copy(connected = false)
    }

    companion object {
        private val MAGIC = byteArrayOf(0x02, 0x01, 0x04, 0x03, 0x06, 0x05, 0x08, 0x07)
        private const val TLV_DETECTED_POINTS = 1

        val MMWAVE_FULL = """
            sensorStop
            flushCfg
            dfeDataOutputMode 1
            channelCfg 15 7 0
            adcCfg 2 1
            adcbufCfg -1 0 1 1 1
            profileCfg 0 60 359 7 57.14 0 0 70 1 256 5209 0 0 158
            chirpCfg 0 0 0 0 0 0 0 1
            chirpCfg 1 1 0 0 0 0 0 4
            frameCfg 0 2 16 0 100 1 0
            lowPower 0 0
            guiMonitor -1 1 0 0 0 0 1
            cfarCfg -1 0 2 8 4 3 0 15 1
            clutterRemoval -1 1
            sensorStart
        """.trimIndent()

        val MMWAVE_REDUCED = """
            sensorStop
            flushCfg
            dfeDataOutputMode 1
            channelCfg 7 3 0
            adcCfg 2 1
            adcbufCfg -1 0 1 1 1
            profileCfg 0 60 359 7 57.14 0 0 40 1 128 3000 0 0 158
            chirpCfg 0 0 0 0 0 0 0 1
            frameCfg 0 1 8 0 250 1 0
            lowPower 0 1
            cfarCfg -1 0 2 8 4 3 0 15 1
            clutterRemoval -1 1
            sensorStart
        """.trimIndent()
    }
}

// ======================================================================
// BLE
// ======================================================================

class BleScanner(private val context: Context) {

    private val _beacons = MutableStateFlow<Map<String, BleBeacon>>(emptyMap())
    val beacons: StateFlow<Map<String, BleBeacon>> = _beacons.asStateFlow()

    private val _health = MutableStateFlow(SensorHealth("ble"))
    val health: StateFlow<SensorHealth> = _health.asStateFlow()

    /** Known token positions, used for RSSI multilateration. */
    val tokens = mutableMapOf<String, Triple<String, Float, Float>>()

    private val smoothed = mutableMapOf<String, Double>()
    private var scanner: android.bluetooth.le.BluetoothLeScanner? = null

    private val callback = object : ScanCallback() {
        override fun onScanResult(callbackType: Int, result: ScanResult) {
            val address = result.device.address ?: return
            // Exponential moving average: raw RSSI is far too jittery to
            // trilaterate with directly (+/- 8 dB frame to frame is normal).
            val previous = smoothed[address] ?: result.rssi.toDouble()
            val value = 0.65 * previous + 0.35 * result.rssi
            smoothed[address] = value

            _beacons.value = _beacons.value + (address to BleBeacon(
                address = address,
                rssi = value.toInt(),
                name = runCatching { result.device.name }.getOrNull() ?: "",
                timestamp = System.currentTimeMillis(),
                txPower = if (result.txPower != ScanResult.TX_POWER_NOT_PRESENT) result.txPower else -59,
            ))
            _health.value = _health.value.copy(
                samples = _health.value.samples + 1,
                lastSampleAt = System.currentTimeMillis(),
            )
        }

        override fun onScanFailed(errorCode: Int) {
            Log.e(TAG, "BLE scan failed: $errorCode")
            _health.value = _health.value.copy(
                connected = false,
                errors = _health.value.errors + 1,
                detail = "scan failed $errorCode",
            )
        }
    }

    fun start(filters: List<ScanFilter> = emptyList()) {
        val manager = context.getSystemService(Context.BLUETOOTH_SERVICE) as? BluetoothManager
        scanner = manager?.adapter?.bluetoothLeScanner
        if (scanner == null) {
            _health.value = _health.value.copy(connected = false, detail = "no BLE adapter")
            return
        }
        val settings = ScanSettings.Builder()
            .setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY)
            .setCallbackType(ScanSettings.CALLBACK_TYPE_ALL_MATCHES)
            .setReportDelay(0)
            .build()
        runCatching { scanner?.startScan(filters, settings, callback) }
            .onSuccess { _health.value = _health.value.copy(connected = true, detail = "scanning") }
            .onFailure { _health.value = _health.value.copy(connected = false, detail = it.message ?: "error") }
    }

    fun stop() {
        runCatching { scanner?.stopScan(callback) }
        _health.value = _health.value.copy(connected = false)
    }

    fun addToken(address: String, label: String, x: Float, y: Float) {
        tokens[address] = Triple(label, x, y)
    }

    /**
     * Weighted least-squares position from >= 3 known tokens.
     * Returns (x, y, sigma) or null when there are too few references.
     */
    fun multilaterate(): Triple<Float, Float, Float>? {
        val known = _beacons.value.values.mapNotNull { beacon ->
            tokens[beacon.address]?.let { beacon to it }
        }
        if (known.size < 3) return null

        val (first, firstToken) = known[0]
        val x0 = firstToken.second
        val y0 = firstToken.third
        val r0 = first.distance

        val rows = known.drop(1)
        val a = Array(rows.size) { DoubleArray(2) }
        val b = DoubleArray(rows.size)
        val w = DoubleArray(rows.size)
        rows.forEachIndexed { i, (beacon, token) ->
            val x = token.second.toDouble()
            val y = token.third.toDouble()
            val r = beacon.distance
            a[i][0] = 2.0 * (x - x0)
            a[i][1] = 2.0 * (y - y0)
            b[i] = r0 * r0 - r * r + x * x - x0 * x0 + y * y - y0 * y0
            w[i] = 1.0 / maxOf(0.5, r)
        }

        // normal equations of the weighted system (2x2, solved directly)
        var a11 = 0.0; var a12 = 0.0; var a22 = 0.0; var b1 = 0.0; var b2 = 0.0
        for (i in rows.indices) {
            val weight = w[i] * w[i]
            a11 += weight * a[i][0] * a[i][0]
            a12 += weight * a[i][0] * a[i][1]
            a22 += weight * a[i][1] * a[i][1]
            b1 += weight * a[i][0] * b[i]
            b2 += weight * a[i][1] * b[i]
        }
        val determinant = a11 * a22 - a12 * a12
        if (abs(determinant) < 1e-9) return null
        val x = (b1 * a22 - b2 * a12) / determinant
        val y = (a11 * b2 - a12 * b1) / determinant

        val residuals = known.map { (beacon, token) ->
            abs(hypot(x - token.second, y - token.third) - beacon.distance)
        }
        val sigma = maxOf(0.8, residuals.average())
        return Triple(x.toFloat(), y.toFloat(), sigma.toFloat())
    }
}

// ======================================================================
// IMU
// ======================================================================

class ImuManager(context: Context) : SensorEventListener {

    private val sensorManager = context.getSystemService(Context.SENSOR_SERVICE) as SensorManager
    private val gyroscope: Sensor? = sensorManager.getDefaultSensor(Sensor.TYPE_GYROSCOPE)
    private val accelerometer: Sensor? = sensorManager.getDefaultSensor(Sensor.TYPE_ACCELEROMETER)
    private val magnetometer: Sensor? = sensorManager.getDefaultSensor(Sensor.TYPE_MAGNETIC_FIELD)

    private val gyro = FloatArray(3)
    private val accel = FloatArray(3)
    private val mag = FloatArray(3)

    private val _samples = MutableSharedFlow<ImuSample>(replay = 1, extraBufferCapacity = 16)
    val samples: SharedFlow<ImuSample> = _samples.asSharedFlow()

    private val _health = MutableStateFlow(SensorHealth("imu"))
    val health: StateFlow<SensorHealth> = _health.asStateFlow()

    private val staticDetector = StaticDetector()

    /** True only when the device has been genuinely still for a whole window. */
    val isStatic: Boolean get() = staticDetector.isStatic

    fun start() {
        val rate = SensorManager.SENSOR_DELAY_GAME   // ~50 Hz, a good power/accuracy trade
        gyroscope?.let { sensorManager.registerListener(this, it, rate) }
        accelerometer?.let { sensorManager.registerListener(this, it, rate) }
        magnetometer?.let { sensorManager.registerListener(this, it, rate) }
        _health.value = _health.value.copy(
            connected = gyroscope != null && accelerometer != null,
            detail = listOfNotNull(
                gyroscope?.let { "gyro" },
                accelerometer?.let { "accel" },
                magnetometer?.let { "mag" },
            ).joinToString("+"),
        )
    }

    override fun onSensorChanged(event: SensorEvent) {
        when (event.sensor.type) {
            Sensor.TYPE_GYROSCOPE -> System.arraycopy(event.values, 0, gyro, 0, 3)
            Sensor.TYPE_ACCELEROMETER -> System.arraycopy(event.values, 0, accel, 0, 3)
            Sensor.TYPE_MAGNETIC_FIELD -> System.arraycopy(event.values, 0, mag, 0, 3)
            else -> return
        }
        if (event.sensor.type != Sensor.TYPE_ACCELEROMETER) return

        val sample = ImuSample(
            timestamp = System.currentTimeMillis(),
            gyro = gyro.copyOf(),
            accel = accel.copyOf(),
            mag = mag.copyOf(),
        )
        staticDetector.push(sample)
        _samples.tryEmit(sample)
        _health.value = _health.value.copy(
            samples = _health.value.samples + 1,
            lastSampleAt = sample.timestamp,
        )
    }

    override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) = Unit

    fun stop() {
        sensorManager.unregisterListener(this)
        _health.value = _health.value.copy(connected = false)
    }
}

/**
 * Windowed zero-velocity detector.
 *
 * A single-sample test ("is |a| about 1 g?") fires during the stance phase of
 * every stride, which clamps the EKF velocity and makes the position estimate
 * lag metres behind reality. Requiring a low-variance *window* fixes that.
 */
class StaticDetector(
    private val window: Int = 12,
    private val accelVarianceMax: Float = 0.09f,
    private val gyroMax: Float = 0.06f,
) {
    private val accelMagnitudes = ArrayDeque<Float>()
    private val gyroMagnitudes = ArrayDeque<Float>()

    var isStatic: Boolean = false
        private set

    fun push(sample: ImuSample): Boolean {
        val accelMagnitude = sqrt(
            sample.accel[0] * sample.accel[0] +
                sample.accel[1] * sample.accel[1] +
                sample.accel[2] * sample.accel[2]
        )
        val gyroMagnitude = sqrt(
            sample.gyro[0] * sample.gyro[0] +
                sample.gyro[1] * sample.gyro[1] +
                sample.gyro[2] * sample.gyro[2]
        )
        accelMagnitudes.addLast(accelMagnitude)
        gyroMagnitudes.addLast(gyroMagnitude)
        if (accelMagnitudes.size > window) {
            accelMagnitudes.removeFirst()
            gyroMagnitudes.removeFirst()
        }
        if (accelMagnitudes.size < window) {
            isStatic = false
            return false
        }
        val mean = accelMagnitudes.average().toFloat()
        val variance = accelMagnitudes.map { (it - mean) * (it - mean) }.average().toFloat()
        val gyroPeak = gyroMagnitudes.max()
        isStatic = variance < accelVarianceMax &&
            gyroPeak < gyroMax &&
            abs(mean - 9.80665f) < 0.4f
        return isStatic
    }

    fun reset() {
        accelMagnitudes.clear()
        gyroMagnitudes.clear()
        isStatic = false
    }
}
