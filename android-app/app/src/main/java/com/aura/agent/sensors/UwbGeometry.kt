package com.aura.agent.sensors

import kotlin.math.max
import kotlin.math.sqrt

/**
 * Pure UWB parsing and geometry — no Android, no coroutines, no I/O.
 *
 * This is deliberately split out of [UwbManager]. The manager owns a
 * `Context`, a serial transport and a coroutine scope, none of which exist on
 * a host JVM, which would make the two pieces of logic most likely to be
 * *wrong* — the wire-format parser and the trilateration solve — also the two
 * pieces impossible to test without an emulator.
 *
 * Everything here is deterministic and side-effect free, so
 * `tools/run-kotlin-tests.sh` can verify it on any machine.
 */
object UwbGeometry {

    /** Result of parsing one DWM3000 shell line. */
    data class Parsed(
        val ranges: Map<String, Float>,
        val lineOfSight: Map<String, Boolean>,
        val cirAmplitude: Float,
        val cirPhase: Float,
    ) {
        val isEmpty: Boolean get() = ranges.isEmpty() && cirAmplitude == 0f
    }

    /**
     * Parse one DWM3000 shell line.
     *
     * Format: `ANCHOR-A=3.214,ANCHOR-B=7.882;CIR=0.42,1.87`, matching the
     * Python driver's hardware path so both sides consume the same firmware
     * output. A trailing `*` on a range marks a non-line-of-sight link.
     *
     * Returns null for a line carrying neither a range nor a CIR sample.
     * Malformed tokens are skipped rather than throwing: serial framing errors
     * put garbage on the wire regularly, and one bad line must not kill the
     * reader loop.
     */
    fun parseLine(line: String): Parsed? {
        val trimmed = line.trim()
        if (trimmed.isEmpty()) return null

        val ranges = LinkedHashMap<String, Float>()
        val los = LinkedHashMap<String, Boolean>()
        var amplitude = 0f
        var phase = 0f

        val head = trimmed.substringBefore(';')
        val cir = trimmed.substringAfter(';', "")

        for (token in head.split(',')) {
            if ('=' !in token) continue
            val key = token.substringBefore('=').trim()
            val raw = token.substringAfter('=').trim()
            if (key.isEmpty() || raw.isEmpty()) continue
            val nlos = raw.endsWith("*")
            val value = raw.removeSuffix("*").trim().toFloatOrNull() ?: continue
            // A negative or absurd range is a framing artefact, not a distance.
            if (value < 0f || value > MAX_RANGE_M) continue
            ranges[key] = value
            los[key] = !nlos
        }

        if (cir.startsWith("CIR=")) {
            val parts = cir.removePrefix("CIR=").split(',')
            amplitude = parts.getOrNull(0)?.trim()?.toFloatOrNull() ?: 0f
            phase = parts.getOrNull(1)?.trim()?.toFloatOrNull() ?: 0f
        }

        val parsed = Parsed(ranges, los, amplitude, phase)
        return if (parsed.isEmpty) null else parsed
    }

    /**
     * Least-squares trilateration from >= 3 anchor ranges.
     *
     * Subtracting the first sphere's equation from each of the others removes
     * the quadratic term and leaves an over-determined linear system, solved
     * here via the 2x2 normal equations.
     *
     * All arithmetic is done in `Double`. The intermediate `b` term contains
     * differences of squared coordinates, which in `Float` loses roughly three
     * significant digits once anchors sit tens of metres from the origin —
     * enough to move the fix by centimetres for no reason.
     *
     * Returns null when fewer than three anchors are known, or when the anchor
     * geometry is degenerate (collinear). With two ranges the solution set is
     * a pair of points, and with collinear anchors it is a reflected pair;
     * emitting one of them as a fix would be a confident lie, and the EKF has
     * no way to tell it from a good measurement.
     */
    fun trilaterate(
        anchors: Map<String, FloatArray>,
        ranges: Map<String, Float>,
    ): FloatArray? {
        val usable = ranges.mapNotNull { (id, r) -> anchors[id]?.let { it to r.toDouble() } }
        // Two anchors leave the normal-equation matrix rank-1, which the
        // conditioning gate below also rejects (verified over 500k random
        // rank-1 layouts: zero survivors). The explicit count check is kept
        // anyway — it states the requirement directly instead of relying on a
        // numerical side effect, and it avoids the wasted solve.
        if (usable.size < 3) return null

        val (first, r0) = usable[0]
        val x0 = first[0].toDouble()
        val y0 = first[1].toDouble()

        var a11 = 0.0; var a12 = 0.0; var a22 = 0.0; var b1 = 0.0; var b2 = 0.0
        for (i in 1 until usable.size) {
            val (anchor, r) = usable[i]
            val ax = 2.0 * (anchor[0].toDouble() - x0)
            val ay = 2.0 * (anchor[1].toDouble() - y0)
            val b = r0 * r0 - r * r +
                anchor[0].toDouble() * anchor[0].toDouble() - x0 * x0 +
                anchor[1].toDouble() * anchor[1].toDouble() - y0 * y0
            a11 += ax * ax; a12 += ax * ay; a22 += ay * ay
            b1 += ax * b;  b2 += ay * b
        }

        val determinant = a11 * a22 - a12 * a12
        if (determinant == 0.0) return null

        // Reject ill-conditioned anchor geometry.
        //
        // Testing the determinant — against a fixed epsilon *or* against the
        // matrix scale — does not work here, and the failure is dangerous
        // rather than merely imprecise. Measured on the 2x2 normal equations:
        //
        //   geometry                  |det|/scale   cond    error @1cm noise
        //   10 m square                     0.75       3           0.014 m
        //   0.2 m triangle                  1.00       1           0.013 m
        //   60 m x 0.5 m sliver             0.20   9.0e4           1.67  m
        //   60 m x 1 mm sliver              0.20   2.3e10        832     m
        //
        // The near-collinear sliver has a perfectly healthy determinant ratio
        // and a determinant of ~1e5, so both tests pass it, yet a centimetre
        // of range noise moves the fix by the better part of a kilometre. The
        // ratio is blind to it because it is invariant to how *thin* the
        // triangle is. The condition number is not, and it also predicts the
        // error growth, so that is what is tested.
        val trace = a11 + a22
        val disc = sqrt(max(trace * trace - 4.0 * determinant, 0.0))
        val lambdaMax = (trace + disc) / 2.0
        val lambdaMin = (trace - disc) / 2.0
        if (lambdaMin <= 0.0 || lambdaMax / lambdaMin > MAX_CONDITION) return null

        return floatArrayOf(
            ((b1 * a22 - b2 * a12) / determinant).toFloat(),
            ((a11 * b2 - a12 * b1) / determinant).toFloat(),
        )
    }

    /** Ranges beyond this are framing artefacts; DW3000 tops out far below. */
    const val MAX_RANGE_M = 1000f

    /**
     * Largest acceptable condition number of the normal-equation matrix.
     *
     * Well-shaped rigs measure 1-3 regardless of scale. 1000 leaves generous
     * headroom for awkward but usable layouts while still rejecting the
     * slivers, where range noise is amplified by orders of magnitude
     * (a 60 m x 0.5 m triangle sits at 9e4 and turns 1 cm into 1.7 m).
     */
    const val MAX_CONDITION = 1000.0
}
