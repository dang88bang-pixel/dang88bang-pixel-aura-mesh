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
    private val vendorId: Int = 0,
    private val productId: Int = 0,
    private val baudRate: Int = 115200,
    private val portIndex: Int = 0,
    private val readTimeoutMs: Int = 200,
    private val writeTimeoutMs: Int = 1000,
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
            serialPort.setParameters(
                baudRate,
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
                "(${driver.device.vendorId}:${driver.device.productId}) @$baudRate"
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
        // vendorId == 0 means "first serial device we can find", which is the
        // right default when only one accessory is plugged in.
        if (vendorId == 0) return drivers.first()
        return drivers.firstOrNull {
            it.device.vendorId == vendorId &&
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
        // USB IDs matching res/xml/usb_device_filter.xml
        const val VID_SILICON_LABS = 0x10C4   // CP210x - RPLIDAR A1/A2
        const val PID_CP2102 = 0xEA60
        const val VID_FTDI = 0x0403           // FT232 - RPLIDAR S2, DWM3000
        const val VID_TI = 0x0451             // XDS110 - IWR6843
        const val VID_REALTEK = 0x0BDA        // RTL2838 - RTL-SDR

        /** RPLIDAR: 115200 for A1/A2, 256000 for S2. */
        fun forLidar(context: Context, baud: Int = 115200) =
            UsbSerialTransport(context, VID_SILICON_LABS, 0, baud)

        /**
         * IWR6843: port 0 is the CLI UART, port 1 the DATA UART.
         * Configuration goes to the CLI; TLV frames arrive on DATA.
         */
        fun forMmwaveCli(context: Context) =
            UsbSerialTransport(context, VID_TI, 0, 115200, portIndex = 0)

        fun forMmwaveData(context: Context) =
            UsbSerialTransport(context, VID_TI, 0, 921600, portIndex = 1)

        fun forUwb(context: Context) =
            UsbSerialTransport(context, VID_FTDI, 0, 115200)
    }
}
