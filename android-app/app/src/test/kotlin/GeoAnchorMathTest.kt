import com.aura.agent.sensors.GeoAnchorMath
import kotlin.math.abs
import kotlin.system.exitProcess

/**
 * Host-JVM tests for the geo-anchor arithmetic.
 *
 * These cover the parts that are easy to get quietly wrong and impossible to
 * notice in the field: a map rotated by the magnetic declination, an anchor
 * taken from a bad fix, and an exported accuracy that ignores the anchor.
 */

private var checks = 0
private var failures = 0

private fun check(name: String, condition: Boolean) {
    checks++
    if (!condition) {
        failures++
        println("  FAIL  $name")
    }
}

private fun near(name: String, actual: Double, expected: Double, tol: Double = 1e-6) {
    check("$name (got $actual, want $expected)", abs(actual - expected) <= tol)
}

fun main() {
    println("== true bearing: declination must be applied ==")
    // Central Europe: ~+4 deg east. A map anchored on magnetic north is
    // rotated by that much -- ~7 m of cross-track error at 100 m.
    near("magnetic 0 + 4 deg -> 4", GeoAnchorMath.trueBearing(0.0, 4.0), 4.0)
    near("magnetic 358 + 4 -> 2 (wraps)", GeoAnchorMath.trueBearing(358.0, 4.0), 2.0)
    near("magnetic 10 + -15 -> 355 (wraps negative)",
        GeoAnchorMath.trueBearing(10.0, -15.0), 355.0)
    near("zero declination is identity", GeoAnchorMath.trueBearing(123.4, 0.0), 123.4)
    check("always in [0,360)", (0..359).all {
        val b = GeoAnchorMath.trueBearing(it.toDouble(), -20.0)
        b >= 0.0 && b < 360.0
    })
    check("non-finite input does not propagate NaN",
        GeoAnchorMath.trueBearing(Double.NaN, 4.0) == 0.0)

    println("== fix acceptance: rejecting is the safe outcome ==")
    check("a good fix is accepted",
        GeoAnchorMath.isAcceptableFix(3.0, 1_000L, 52.3759, 9.7320))
    check("a 40 m fix is rejected",
        !GeoAnchorMath.isAcceptableFix(40.0, 1_000L, 52.0, 9.0))
    check("a stale fix is rejected (could be another building)",
        !GeoAnchorMath.isAcceptableFix(3.0, 120_000L, 52.0, 9.0))
    check("zero accuracy is rejected, not treated as perfect",
        !GeoAnchorMath.isAcceptableFix(0.0, 1_000L, 52.0, 9.0))
    check("NaN accuracy is rejected",
        !GeoAnchorMath.isAcceptableFix(Double.NaN, 1_000L, 52.0, 9.0))
    check("out-of-range latitude is rejected",
        !GeoAnchorMath.isAcceptableFix(3.0, 1_000L, 91.0, 9.0))
    check("negative age is rejected",
        !GeoAnchorMath.isAcceptableFix(3.0, -5L, 52.0, 9.0))
    check("exactly at the limit is accepted",
        GeoAnchorMath.isAcceptableFix(GeoAnchorMath.MAX_ANCHOR_SIGMA_M, 0L, 52.0, 9.0))

    println("== combined sigma: the anchor usually dominates ==")
    // EKF 0.06 m relative to origin, anchor 5 m -> the position is a 5 m one.
    near("0.06 (+) 5.0", GeoAnchorMath.combinedSigma(0.06, 5.0), 5.00036, 1e-4)
    near("quadrature, not sum", GeoAnchorMath.combinedSigma(3.0, 4.0), 5.0, 1e-9)
    near("zero anchor leaves the EKF term", GeoAnchorMath.combinedSigma(0.25, 0.0), 0.25)
    near("zero EKF leaves the anchor term", GeoAnchorMath.combinedSigma(0.0, 4.0), 4.0)
    check("combined is never smaller than either term",
        GeoAnchorMath.combinedSigma(0.5, 2.0) >= 2.0)
    check("NaN does not poison the result",
        GeoAnchorMath.combinedSigma(Double.NaN, 4.0) == 4.0)

    println("== ce conversion must match the Python reference ==")
    // aura/cot.py: _sigma_to_ce uses the Rayleigh 95% factor 2.4477.
    near("1 sigma -> ce", GeoAnchorMath.sigmaToCe(1.0), 2.4477, 1e-9)
    near("0.25 sigma -> ce", GeoAnchorMath.sigmaToCe(0.25), 0.611925, 1e-9)

    println("== dominance hint for the operator ==")
    check("5 m anchor dominates a 0.06 m EKF",
        GeoAnchorMath.anchorDominates(0.06, 5.0))
    check("a surveyed anchor does not dominate",
        !GeoAnchorMath.anchorDominates(0.5, 0.05))

    println()
    println("----------------------------------------")
    println("$checks checks, $failures failures")
    if (failures > 0) exitProcess(1)
}
