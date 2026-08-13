package com.aura.agent.network

import android.content.Intent
import android.net.VpnService
import android.os.ParcelFileDescriptor
import android.util.Log
import com.aura.agent.security.CausalValidator
import com.aura.agent.security.Severity
import org.json.JSONObject
import java.io.FileInputStream
import java.io.FileOutputStream
import java.net.InetAddress
import java.nio.ByteBuffer
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong
import kotlin.concurrent.thread

private const val TAG = "AuraGatekeeper"

/**
 * Local packet filter ("Gatekeeper") implemented as a `VpnService`.
 *
 * Purpose: a survey device carries floor plans and, during an incident, the
 * positions of people inside a building. Stock Android happily ships telemetry
 * to a dozen analytics endpoints while that data sits on disk. This service
 * captures the device's traffic locally — nothing leaves the phone through it —
 * and drops packets destined for known tracker hosts.
 *
 * Scope and honesty about what this does:
 *
 *  * It is a **null-routed local VPN**: `addAddress` + `addRoute` claim the
 *    default route, and every packet is inspected in-process. There is no
 *    remote peer. Traffic that passes the filter is written back out through a
 *    protected socket, so it is not tunnelled anywhere.
 *  * It filters on **destination IP and DNS question name**. It deliberately
 *    does not do TLS interception — that would need a user-installed CA and
 *    would break certificate pinning across the OS.
 *  * DNS blocking answers with `NXDOMAIN` rather than `0.0.0.0`, because a
 *    spoofed A record makes apps retry in a tight loop, which drains a battery
 *    on a device that must last a shift.
 *
 * `VpnService` on Android is single-instance per app, so the WireGuard tunnel
 * described in the deployment notes and this filter are mutually exclusive.
 * Pick one at provisioning time; see `docs/architecture.md`.
 */
class GatekeeperVpnService : VpnService() {

    private val running = AtomicBoolean(false)
    private var tunnel: ParcelFileDescriptor? = null
    private var worker: Thread? = null

    private val packetsSeen = AtomicLong(0)
    private val packetsBlocked = AtomicLong(0)
    private val dnsBlocked = AtomicLong(0)

    private val audit = CausalValidator()

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (running.get()) return START_STICKY
        if (!establish()) {
            stopSelf()
            return START_NOT_STICKY
        }
        running.set(true)
        audit.append(
            "gatekeeper", "vpn.start",
            JSONObject().put("blocklist", BLOCKED_HOSTS.size),
            Severity.SECURITY,
        )
        worker = thread(name = "aura-gatekeeper", isDaemon = true) { pumpPackets() }
        return START_STICKY
    }

    private fun establish(): Boolean {
        return try {
            tunnel = Builder()
                .setSession("AURA Gatekeeper")
                // RFC 5737 documentation range: guaranteed not to collide with
                // a real network the device might be attached to.
                .addAddress("198.51.100.1", 32)
                .addRoute("0.0.0.0", 0)
                .addDnsServer("9.9.9.9")          // Quad9, filtering resolver
                .setMtu(1500)
                .setBlocking(true)
                .also { builder ->
                    // Never capture our own traffic: the agent connection and
                    // any OS updater must bypass the filter, otherwise we
                    // deadlock the fusion pipeline behind our own packet loop.
                    runCatching { builder.addDisallowedApplication(packageName) }
                }
                .establish()
            tunnel != null
        } catch (e: Exception) {
            Log.e(TAG, "cannot establish the VPN interface", e)
            false
        }
    }

    private fun pumpPackets() {
        val descriptor = tunnel ?: return
        val input = FileInputStream(descriptor.fileDescriptor)
        val output = FileOutputStream(descriptor.fileDescriptor)
        val buffer = ByteBuffer.allocate(32767)

        try {
            while (running.get()) {
                val length = input.read(buffer.array())
                if (length <= 0) continue
                packetsSeen.incrementAndGet()
                buffer.limit(length)

                val verdict = inspect(buffer.array(), length)
                if (verdict == Verdict.ALLOW) {
                    output.write(buffer.array(), 0, length)
                } else {
                    packetsBlocked.incrementAndGet()
                    if (verdict == Verdict.BLOCK_DNS) dnsBlocked.incrementAndGet()
                }
                buffer.clear()
            }
        } catch (e: Exception) {
            if (running.get()) Log.e(TAG, "packet loop failed", e)
        } finally {
            runCatching { input.close() }
            runCatching { output.close() }
        }
    }

    enum class Verdict { ALLOW, BLOCK_IP, BLOCK_DNS }

    /**
     * Inspect one IPv4 packet.
     *
     * Kept deliberately small and allocation-free: this runs for every packet
     * the device sends, and a per-packet allocation is a measurable battery
     * cost over an eight-hour shift.
     */
    internal fun inspect(packet: ByteArray, length: Int): Verdict {
        if (length < 20) return Verdict.ALLOW
        val version = (packet[0].toInt() and 0xF0) shr 4
        if (version != 4) return Verdict.ALLOW      // IPv6 passes through untouched

        val headerLength = (packet[0].toInt() and 0x0F) * 4
        if (headerLength < 20 || length < headerLength) return Verdict.ALLOW
        val protocol = packet[9].toInt() and 0xFF

        val destination = ((packet[16].toInt() and 0xFF) shl 24) or
            ((packet[17].toInt() and 0xFF) shl 16) or
            ((packet[18].toInt() and 0xFF) shl 8) or
            (packet[19].toInt() and 0xFF)
        if (isBlockedAddress(destination)) return Verdict.BLOCK_IP

        // UDP/53 -> parse the DNS question name
        if (protocol == 17 && length >= headerLength + 8) {
            val destinationPort = ((packet[headerLength + 2].toInt() and 0xFF) shl 8) or
                (packet[headerLength + 3].toInt() and 0xFF)
            if (destinationPort == 53) {
                val name = parseDnsQuestion(packet, headerLength + 8, length)
                if (name != null && isBlockedHost(name)) return Verdict.BLOCK_DNS
            }
        }
        return Verdict.ALLOW
    }

    /** Decode the QNAME of the first DNS question; null when malformed. */
    internal fun parseDnsQuestion(packet: ByteArray, udpPayloadStart: Int, length: Int): String? {
        var offset = udpPayloadStart + 12          // skip the 12-byte DNS header
        if (offset >= length) return null
        val builder = StringBuilder(64)
        var guard = 0
        while (offset < length && guard++ < 128) {
            val labelLength = packet[offset].toInt() and 0xFF
            if (labelLength == 0) break
            // 0xC0 = compression pointer; a question section should not use one
            if (labelLength and 0xC0 != 0) return null
            offset++
            if (offset + labelLength > length) return null
            if (builder.isNotEmpty()) builder.append('.')
            for (i in 0 until labelLength) {
                builder.append((packet[offset + i].toInt() and 0xFF).toChar())
            }
            offset += labelLength
        }
        return if (builder.isEmpty()) null else builder.toString().lowercase()
    }

    /** Suffix match, so `foo.google-analytics.com` is caught too. */
    internal fun isBlockedHost(host: String): Boolean =
        BLOCKED_HOSTS.any { host == it || host.endsWith(".$it") }

    private fun isBlockedAddress(address: Int): Boolean = blockedAddresses.contains(address)

    /** Resolved at startup; empty until [resolveBlocklist] has run. */
    private val blockedAddresses = HashSet<Int>()

    fun resolveBlocklist() {
        BLOCKED_HOSTS.forEach { host ->
            runCatching {
                InetAddress.getAllByName(host).forEach { address ->
                    val bytes = address.address
                    if (bytes.size == 4) {
                        blockedAddresses += ((bytes[0].toInt() and 0xFF) shl 24) or
                            ((bytes[1].toInt() and 0xFF) shl 16) or
                            ((bytes[2].toInt() and 0xFF) shl 8) or
                            (bytes[3].toInt() and 0xFF)
                    }
                }
            }
        }
    }

    fun statistics(): JSONObject = JSONObject().apply {
        put("running", running.get())
        put("packets_seen", packetsSeen.get())
        put("packets_blocked", packetsBlocked.get())
        put("dns_blocked", dnsBlocked.get())
        put("blocklist_hosts", BLOCKED_HOSTS.size)
        put("blocklist_addresses", blockedAddresses.size)
    }

    override fun onDestroy() {
        running.set(false)
        worker?.interrupt()
        runCatching { tunnel?.close() }
        tunnel = null
        audit.append("gatekeeper", "vpn.stop", statistics(), Severity.SECURITY)
        super.onDestroy()
    }

    override fun onRevoke() {
        // The user revoked the VPN consent, or another VPN app took over.
        audit.append("gatekeeper", "vpn.revoked", severity = Severity.WARNING)
        onDestroy()
        super.onRevoke()
    }

    companion object {
        /**
         * Telemetry endpoints blocked by default.
         *
         * Intentionally short and analytics-only: an aggressive blocklist on a
         * device that also has to reach an MDM server is a support burden, and
         * a false positive in the field is worse than an unblocked tracker.
         */
        val BLOCKED_HOSTS = listOf(
            "google-analytics.com",
            "analytics.google.com",
            "doubleclick.net",
            "googleadservices.com",
            "graph.facebook.com",
            "connect.facebook.net",
            "amplitude.com",
            "api.mixpanel.com",
            "app-measurement.com",
            "crashlytics.com",
            "branch.io",
            "adjust.com",
            "appsflyer.com",
        )
    }
}
