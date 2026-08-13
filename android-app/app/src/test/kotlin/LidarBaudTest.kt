import com.aura.agent.sensors.LidarBaud

/**
 * Host-side tests for the RPLIDAR baud picker.
 *
 * A wrong rate does not throw. The port opens, the driver looks healthy, and
 * every scan is noise. These tests exist so a "just use 256000 for anything
 * FTDI" change cannot land unnoticed — that figure is the S1/A3 rate, not
 * the S2's.
 */

private var checks = 0
private var failures = 0

private fun check(condition: Boolean, what: String) {
    checks++
    if (!condition) {
        failures++
        println("  FAIL: $what")
    }
}

fun main() {
    println("AURA 6.0 - LiDAR baud suite")

    check(LidarBaud.A_SERIES == 115_200, "A-series is 115200")
    check(LidarBaud.S2 == 1_000_000, "S2 is 1 Mbaud, not 256000")
    check(LidarBaud.S2 != 256_000, "256000 is a different product (S1/A3)")

    check(LidarBaud.forVendor(0x10C4) == LidarBaud.A_SERIES, "CP2102 -> A-series")
    check(LidarBaud.forVendor(0x0403) == LidarBaud.S2, "FT232 -> S2")
    check(LidarBaud.forVendor(0x0403, override = 115_200) == 115_200, "explicit override wins")
    check(LidarBaud.forVendor(0x10C4, override = 0) == LidarBaud.A_SERIES, "override 0 means auto")

    // The constant must stay equal to UsbSerialTransport.VID_FTDI. We cannot
    // import that class here (it pulls in android.hardware.usb), so the
    // numeric value is pinned and tools/check-usb-ids.py watches the other
    // copy.
    check(LidarBaud.VID_FTDI == 0x0403, "VID_FTDI matches the USB-IF FTDI id")

    println("\n----------------------------------------")
    println("$checks checks, $failures failures")
    if (failures > 0) kotlin.system.exitProcess(1)
}
