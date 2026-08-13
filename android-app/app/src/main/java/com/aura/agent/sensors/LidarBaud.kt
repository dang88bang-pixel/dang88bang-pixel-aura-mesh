package com.aura.agent.sensors

/**
 * Serial rates for the RPLIDAR models this project accepts.
 *
 * Kept free of Android types so the host-JVM suite can pin the selection
 * without a USB stack. A wrong rate here is silent: the device enumerates,
 * `open()` succeeds, and every scan is garbage.
 */
object LidarBaud {
    /** A1, A2M6, A2M8 — Silicon Labs CP2102. */
    const val A_SERIES = 115_200

    /**
     * S2 and S3. Slamtec's own FAQ
     * (https://wiki.slamtec.com/display/SD/RPLIDAR+FAQ, read 2026-08-13):
     * "Black housing without DIP switch 1000000:S2,S3" and, later on the
     * same page, "The baud rate for S2 is 1M."
     *
     * 256000 is the S1 / A3 / A2M7 rate. Shipping that for an S2 is how the
     * sensor presents as attached-but-mute.
     */
    const val S2 = 1_000_000

    /**
     * FTDI vendor id. Duplicated from [UsbSerialTransport.VID_FTDI] so this
     * file compiles on a host JVM; the USB-id gate fails if they drift.
     */
    const val VID_FTDI = 0x0403

    /**
     * Pick a rate from the bridge chip when the caller did not override.
     *
     * A1/A2 ship with a CP2102 (0x10C4); the S2 ships with an FT232
     * (0x0403). One rate for both is the silent-failure mode this function
     * exists to close.
     */
    fun forVendor(vendorId: Int, override: Int = 0): Int {
        if (override > 0) return override
        return if (vendorId == VID_FTDI) S2 else A_SERIES
    }
}
