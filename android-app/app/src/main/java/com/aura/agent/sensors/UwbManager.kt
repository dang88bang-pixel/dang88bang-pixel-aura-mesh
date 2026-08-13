package com.aura.agent.sensors

import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
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

private const val TAG = "AuraUwb"

/**
 * UWB ranging and channel-impulse-response capture.
 *
 * ## Why this is not just `androidx.core.uwb`
 *
 * The **Honeywell CT45P-X0N ships Android 11 (API 30)** and has no UWB radio.
 * `androidx.core.uwb` requires **API 31+** and a device with hardware support,
 * so on the actual target it can never be used. Calling into it unguarded is a
 * `NoClassDefFoundError` on the first ranging attempt.
 *
 * The primary path is therefore an **external Qorvo DWM3000 over USB-serial**,
 * which works on API 30. The platform API is used only when the app happens to
 * run on a newer UWB-capable device, and it is reached **reflectively** so the
 * class is never resolved on Android 11.
 *
 * ```
 * API 31+ with hardware  ->  platform UWB   (reflective, optional)
 * anything else          ->  DWM3000 serial (the CT45P path)
 * ```
 *
 * ## Two-way ranging vs. through-wall sensing
 *
 * Ranging to anchors feeds the EKF. Through-wall detection is a different
 * measurement: the chest wall phase-modulates a static multipath tap by
 * fractions of a millimetre, and that shows up in the CIR, not in the range.
 * Both come from the same driver, which is why [UwbReading] carries them
 * together.
 */
class UwbManager(
    private val context: Context,
    private val transport: SerialTransport? = null,
    private val scope: CoroutineScope,
) {

    enum class Backend { NONE, DWM3000_SERIAL, PLATFORM_API }

    private val running = AtomicBoolean(false)
    private var job: Job? = null
    private val buffer = StringBuilder(256)

    private val _readings = MutableSharedFlow<UwbReading>(replay = 1, extraBufferCapacity = 8)
    val readings: SharedFlow<UwbReading> = _readings.asSharedFlow()

    private val _health = MutableStateFlow(SensorHealth("uwb"))
    val health: StateFlow<SensorHealth> = _health.asStateFlow()

    var backend: Backend = Backend.NONE
        private set

    /** Surveyed anchor positions in the map frame. */
    val anchors = mutableMapOf<String, FloatArray>()

    fun addAnchor(id: String, x: Float, y: Float, z: Float) {
        anchors[id] = floatArrayOf(x, y, z)
    }

    // ------------------------------------------------------------------
    fun start(): Backend {
        if (running.get()) return backend

        backend = when {
            transport != null && openSerial() -> Backend.DWM3000_SERIAL
            platformUwbAvailable() -> Backend.PLATFORM_API
            else -> Backend.NONE
        }

        when (backend) {
            Backend.DWM3000_SERIAL -> startSerialLoop()
            Backend.PLATFORM_API -> startPlatformRanging()
            Backend.NONE -> {
                _health.value = _health.value.copy(
                    connected = false,
                    detail = "no UWB: attach a DWM3000, or run on an API 31+ UWB device",
                )
                Log.w(TAG, "no UWB backend available")
            }
        }
        return backend
    }

    private fun openSerial(): Boolean {
        val usb = transport as? UsbSerialTransport ?: return transport?.isOpen == true
        return when (usb.open()) {
            UsbSerialTransport.OpenResult.OPENED -> true
            UsbSerialTransport.OpenResult.PERMISSION_PENDING -> {
                // Retry once the user answers the dialog; do not block here.
                usb.onPermissionResult = { granted -> if (granted && !running.get()) start() }
                _health.value = _health.value.copy(detail = "waiting for USB permission")
                false
            }
            else -> false
        }
    }

    /**
     * Is the platform UWB stack usable?
     *
     * Deliberately reflective: on API 30 the `androidx.core.uwb` classes are
     * absent, and a direct reference would throw `NoClassDefFoundError` at
     * verification time rather than returning false here.
     */
    private fun platformUwbAvailable(): Boolean {
        if (Build.VERSION.SDK_INT < 31) {
            Log.i(TAG, "platform UWB needs API 31+, this device is API ${Build.VERSION.SDK_INT}")
            return false
        }
        if (!context.packageManager.hasSystemFeature("android.hardware.uwb")) {
            Log.i(TAG, "device reports no UWB hardware")
            return false
        }
        return runCatching {
            Class.forName("androidx.core.uwb.UwbManager")
            true
        }.getOrDefault(false)
    }

    // ------------------------------------------------------------------
    private fun startSerialLoop() {
        running.set(true)
        _health.value = _health.value.copy(connected = true, detail = "DWM3000 over serial")

        job = scope.launch(Dispatchers.IO) {
            val chunk = ByteArray(1024)
            transport?.write(CMD_INIT)
            while (isActive && running.get()) {
                transport?.write(CMD_RANGE)
                val read = transport?.read(chunk, 200) ?: -1
                // 0 means "nothing yet", which is normal between epochs.
                if (read <= 0) continue

                buffer.append(String(chunk, 0, read, Charsets.US_ASCII))
                while (true) {
                    val newline = buffer.indexOf("\n")
                    if (newline < 0) break
                    val line = buffer.substring(0, newline).trim()
                    buffer.delete(0, newline + 1)
                    parseLine(line)?.let { reading ->
                        _readings.tryEmit(reading)
                        _health.value = _health.value.copy(
                            samples = _health.value.samples + 1,
                            lastSampleAt = System.currentTimeMillis(),
                        )
                    }
                }
                // Runaway guard: a device spewing bytes without newlines must
                // not grow this buffer without bound.
                if (buffer.length > 8192) buffer.setLength(0)
            }
        }
    }

    /**
     * Parse one DWM3000 shell line.
     *
     * Format: `ANCHOR-A=3.214,ANCHOR-B=7.882;CIR=0.42,1.87`
     * Identical to the Python driver's hardware path, so both sides consume
     * the same firmware output.
     */
    internal fun parseLine(line: String): UwbReading? {
        val parsed = UwbGeometry.parseLine(line) ?: return null
        return UwbReading(
            System.currentTimeMillis(),
            parsed.ranges,
            parsed.cirAmplitude,
            parsed.cirPhase,
            parsed.lineOfSight,
        )
    }

    // ------------------------------------------------------------------
    private fun startPlatformRanging() {
        running.set(true)
        _health.value = _health.value.copy(connected = true, detail = "platform UWB (API 31+)")
        // Reaching androidx.core.uwb reflectively keeps this file loadable on
        // API 30. A device that actually has UWB hardware is out of scope for
        // the CT45P deployment, so this path is intentionally minimal: it
        // reports availability and leaves session setup to the integrator.
        Log.i(TAG, "platform UWB available; session setup is device-specific")
    }

    // ------------------------------------------------------------------
    /**
     * Least-squares trilateration against the configured anchors.
     *
     * The solve itself lives in [UwbGeometry] so it can be tested on a host
     * JVM; see there for the degeneracy and precision rationale.
     */
    fun trilaterate(ranges: Map<String, Float>): FloatArray? =
        UwbGeometry.trilaterate(anchors, ranges)

    fun stop() {
        running.set(false)
        job?.cancel()
        transport?.write(CMD_STOP)
        _health.value = _health.value.copy(connected = false)
    }

    companion object {
        // The **Aura anchor protocol** - ours, not Qorvo's.
        //
        // Stock DWM3001CDK firmware speaks a CLI/UCI console over USB CDC
        // whose command set is defined by that firmware; there is no $RANGE
        // in it and no published wire standard for one. Checked against
        // Qorvo's documentation and forum, 2026-08-13.
        //
        // So this will not talk to an off-the-shelf board. The anchor must run
        // firmware implementing this protocol; see docs/uwb_anchor_protocol.md.
        // Labelled explicitly because calling it "the DWM3000 shell format"
        // invites someone to buy hardware that cannot work.
        private val CMD_INIT = "\$INIT\r\n".toByteArray()
        private val CMD_RANGE = "\$RANGE\r\n".toByteArray()
        private val CMD_STOP = "\$STOP\r\n".toByteArray()
    }
}
