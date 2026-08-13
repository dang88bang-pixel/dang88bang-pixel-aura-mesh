package com.aura.agent.sensors

import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.hardware.usb.UsbDevice
import android.hardware.usb.UsbDeviceConnection
import android.hardware.usb.UsbManager
import android.os.Build
import android.util.Log
import com.hoho.android.usbserial.driver.UsbSerialDriver
import com.hoho.android.usbserial.driver.UsbSerialPort
import com.hoho.android.usbserial.driver.UsbSerialProber
import java.util.concurrent.atomic.AtomicBoolean

private const val TAG = "AuraUsb"
private const val ACTION_USB_PERMISSION = "com.aura.agent.USB_PERMISSION"

/**
 * USB-serial transport for the sensors that hang off the CT45P's USB-C port:
 * RPLIDAR, the TI IWR6843 mmWave front-end, the Qorvo DWM3000 and the SDR.
 *
 * This replaces the earlier placeholder, which declared `InputStream`/
 * `OutputStream` fields and never assigned them — every read returned -1 and
 * every parser sat idle. It is built on `usb-serial-for-android`, which is
 * already a declared dependency.
 *
 * ## Things that bite on real hardware
 *
 * * **Permission is asynchronous.** `UsbManager.requestPermission` shows a
 *   dialog; the device is not usable until the broadcast arrives. [open] is
 *   therefore non-blocking and returns [OpenResult.PERMISSION_PENDING] — call
 *   it again from [onPermissionGranted]. Blocking a sensor thread on the
 *   dialog deadlocks the fusion loop.
 * * **`port.read` timeouts are not errors.** A LiDAR between sweeps returns 0
 *   bytes constantly. Treating that as a failure and reopening the port is a
 *   classic way to make a working device look broken.
 * * **Reads must be bounded.** `read(buf, 0)` blocks forever on some drivers,
 *   which hangs shutdown. We always pass a finite timeout.
 * * **The CDC-ACM prober misses some boards.** The TI XDS110 exposes two
 *   interfaces (CLI + DATA); [portIndex] selects which one.
 */
class UsbSerialTransport(
    private val context: Context,
    /**
     * Accepted USB vendor IDs, tried in order. A set rather than a single ID
     * because the same sensor ships with different bridge chips depending on
     * board revision - the IWR6843ISK uses a SiLabs CP2105 on Rev C/D and a TI
     * XDS110 on earlier boards. Matching only one silently fails to detect
     * half the hardware in the field.
     *
     * Empty means "first serial device found", which is right when only one
     * accessory is attached.
     */
    private val vendorIds: Set<Int> = emptySet(),
    private val productId: Int = 0,
    private val baudRate: Int = 115200,
    private val portIndex: Int = 0,
    private val readTimeoutMs: Int = 200,
    private val writeTimeoutMs: Int = 1000,
    /**
     * Optional per-device rate. Used by [forLidar] so an S2 on an FTDI
     * bridge opens at 1 Mbaud and an A1 on a CP2102 at 115200, without the
     * caller having to know which is plugged in.
     */
    private val resolveBaud: ((vendorId: Int) -> Int)? = null,
) : SerialTransport {

    enum class OpenResult { OPENED, NO_DEVICE, PERMISSION_PENDING, FAILED }

    private var port: UsbSerialPort? = null
    private var connection: UsbDeviceConnection? = null
    private val opened = AtomicBoolean(false)
    private var permissionReceiver: BroadcastReceiver? = null

    /** Invoked once the user grants (or denies) the USB permission dialog. */
    var onPermissionResult: ((granted: Boolean) -> Unit)? = null

    override val isOpen: Boolean get() = opened.get() && port != null

    var lastError: String = ""
        private set

    /** Human-readable description of the attached device, for the UI. */
    var deviceName: String = ""
        private set

    // ------------------------------------------------------------------
    fun open(): OpenResult {
        if (isOpen) return OpenResult.OPENED

        val manager = context.getSystemService(Context.USB_SERVICE) as? UsbManager
            ?: return fail("no UsbManager")

        val driver = findDriver(manager) ?: run {
            lastError = "no matching USB serial device attached"
            return OpenResult.NO_DEVICE
        }

        if (!manager.hasPermission(driver.device)) {
            requestPermission(manager, driver.device)
            lastError = "waiting for USB permission"
            return OpenResult.PERMISSION_PENDING
        }

        return try {
            val usbConnection = manager.openDevice(driver.device)
                ?: return fail("openDevice returned null (permission revoked?)")
            val serialPort = driver.ports.getOrNull(portIndex)
                ?: return fail("device has no port index $portIndex")

            serialPort.open(usbConnection)
            val rate = resolveBaud?.invoke(driver.device.vendorId) ?: baudRate
            serialPort.setParameters(
                rate,
                UsbSerialPort.DATABITS_8,
                UsbSerialPort.STOPBITS_1,
                UsbSerialPort.PARITY_NONE,
            )
            // Some boards (notably CDC-ACM bridges) stay mute until DTR is
            // asserted; failures here are non-fatal on drivers that lack it.
            runCatching { serialPort.dtr = true }
            runCatching { serialPort.rts = true }

            port = serialPort
            connection = usbConnection
            opened.set(true)
            deviceName = "${driver.device.manufacturerName ?: "?"} " +
                "${driver.device.productName ?: "?"} " +
                "(${driver.device.vendorId}:${driver.device.productId}) @$rate"
            lastError = ""
            Log.i(TAG, "opened $deviceName")
            OpenResult.OPENED
        } catch (e: Exception) {
            fail("open failed: ${e.message}")
        }
    }

    private fun findDriver(manager: UsbManager): UsbSerialDriver? {
        val drivers = UsbSerialProber.getDefaultProber().findAllDrivers(manager)
        if (drivers.isEmpty()) return null
        if (vendorIds.isEmpty()) return drivers.first()
        return drivers.firstOrNull {
            it.device.vendorId in vendorIds &&
                (productId == 0 || it.device.productId == productId)
        }
    }

    private fun requestPermission(manager: UsbManager, device: UsbDevice) {
        if (permissionReceiver == null) {
            val receiver = object : BroadcastReceiver() {
                override fun onReceive(context: Context, intent: Intent) {
                    if (intent.action != ACTION_USB_PERMISSION) return
                    val granted = intent.getBooleanExtra(UsbManager.EXTRA_PERMISSION_GRANTED, false)
                    Log.i(TAG, "USB permission ${if (granted) "granted" else "denied"}")
                    onPermissionResult?.invoke(granted)
                }
            }
            permissionReceiver = receiver
            val filter = IntentFilter(ACTION_USB_PERMISSION)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                context.registerReceiver(receiver, filter, Context.RECEIVER_NOT_EXPORTED)
            } else {
                @Suppress("UnspecifiedRegisterReceiverFlag")
                context.registerReceiver(receiver, filter)
            }
        }
        // FLAG_MUTABLE: the system fills in EXTRA_PERMISSION_GRANTED, so an
        // immutable PendingIntent would silently never deliver the result.
        val flags = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            PendingIntent.FLAG_MUTABLE
        } else {
            0
        }
        val intent = PendingIntent.getBroadcast(
            context, 0, Intent(ACTION_USB_PERMISSION).setPackage(context.packageName), flags,
        )
        manager.requestPermission(device, intent)
    }

    /** Retry after the permission dialog resolved. */
    fun onPermissionGranted(): OpenResult = open()

    // ------------------------------------------------------------------
    override fun write(bytes: ByteArray) {
        val serialPort = port ?: return
        try {
            serialPort.write(bytes, writeTimeoutMs)
        } catch (e: Exception) {
            lastError = "write failed: ${e.message}"
            Log.e(TAG, lastError)
        }
    }

    /**
     * Read into [buffer]; returns the byte count, 0 on timeout, -1 on error.
     *
     * A timeout is a normal idle state, not a failure: a LiDAR between sweeps
     * legitimately has nothing to say. Callers must not treat 0 as an error.
     */
    override fun read(buffer: ByteArray, timeoutMs: Int): Int {
        val serialPort = port ?: return -1
        return try {
            serialPort.read(buffer, if (timeoutMs > 0) timeoutMs else readTimeoutMs)
        } catch (e: Exception) {
            lastError = "read failed: ${e.message}"
            Log.e(TAG, lastError)
            -1
        }
    }

    override fun close() {
        opened.set(false)
        runCatching { port?.close() }
        runCatching { connection?.close() }
        port = null
        connection = null
        permissionReceiver?.let { receiver ->
            runCatching { context.unregisterReceiver(receiver) }
            permissionReceiver = null
        }
    }

    private fun fail(message: String): OpenResult {
        lastError = message
        Log.w(TAG, message)
        return OpenResult.FAILED
    }

    companion object {
        // USB vendor IDs. Verified against the USB-IF registry
        // (usb-ids.gowdy.us, 2026-08-13); see docs/source_claims.md.
        /** Silicon Labs - CP210x family (CP2102 single, CP2105 dual UART). */
        const val VID_SILICON_LABS = 0x10C4
        /** Future Technology Devices International - FT232, FT2232. */
        const val VID_FTDI = 0x0403
        /** Texas Instruments - XDS110 debug probe. */
        const val VID_TI = 0x0451
        /** Realtek - RTL2832U/RTL2838 (RTL-SDR). */
        const val VID_REALTEK = 0x0BDA

        const val PID_CP2102 = 0xEA60         // CP210x UART Bridge
        const val PID_CP2105 = 0xEA70         // CP2105 Dual UART Bridge
        const val PID_FT232 = 0x6001          // FT232 Serial (UART) IC
        const val PID_XDS110 = 0xBEF3         // XDS110, exposes 2 UARTs

        /**
         * RPLIDAR: A1/A2 use a CP2102 at 115200; the S2 uses an FTDI bridge at
         * 1 000 000 (Slamtec FAQ, not 256000 — that is the S1/A3 rate).
         * Both vendors are accepted so either model is detected; the rate is
         * picked from the bridge chip unless [baud] is a positive override.
         */
        fun forLidar(context: Context, baud: Int = 0) =
            UsbSerialTransport(
                context,
                setOf(VID_SILICON_LABS, VID_FTDI),
                0,
                if (baud > 0) baud else LidarBaud.A_SERIES,
                resolveBaud = if (baud > 0) null else { vid -> LidarBaud.forVendor(vid) },
            )

        /**
         * IWR6843: port 0 is the CLI UART (configuration), port 1 the DATA
         * UART (TLV frames).
         *
         * Both bridge chips are accepted. TI's own guidance is that Rev C/D
         * IWR6843ISK boards carry a **SiLabs CP2105** ("Enhanced COM Port" =
         * CLI, "Standard COM Port" = data) while earlier boards use the
         * **XDS110** ("Application/User UART" and "Auxiliary Data Port").
         * Accepting only one leaves half the boards undetected, which presents
         * as a sensor that is simply never found.
         */
        fun forMmwaveCli(context: Context) =
            UsbSerialTransport(context, setOf(VID_TI, VID_SILICON_LABS), 0, 115200, portIndex = 0)

        fun forMmwaveData(context: Context) =
            UsbSerialTransport(context, setOf(VID_TI, VID_SILICON_LABS), 0, 921600, portIndex = 1)

        /** DWM3000 evaluation boards ship with an FTDI bridge. */
        fun forUwb(context: Context) =
            UsbSerialTransport(context, setOf(VID_FTDI), 0, 115200)
    }
}
