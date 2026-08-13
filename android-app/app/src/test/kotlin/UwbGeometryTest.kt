import com.aura.agent.sensors.UwbGeometry
import kotlin.math.abs
import kotlin.math.hypot

/**
 * Host-side tests for the UWB wire parser and the trilateration solve.
 *
 * These two functions decide where the operator is on the map. They run on a
 * plain JVM by construction (see UwbGeometry's header), so they are verified
 * here rather than trusted.
 */

private var checks = 0
private var failures = 0

private fun check(name: String, cond: Boolean) {
    checks++
    if (!cond) {
        failures++
        println("  FAIL: $name")
    }
}

private fun near(name: String, actual: Float, expected: Float, tol: Float = 1e-3f) =
    check("$name (expected $expected, got $actual)", abs(actual - expected) <= tol)

private fun section(title: String) = println("\n== $title ==")

fun main() {
    // ---------------------------------------------------------------- parsing
    section("parsing: the documented firmware line")
    val p = UwbGeometry.parseLine("ANCHOR-A=3.214,ANCHOR-B=7.882;CIR=0.42,1.87")
    check("line parses", p != null)
    p!!
    check("two anchors", p.ranges.size == 2)
    near("anchor A range", p.ranges["ANCHOR-A"]!!, 3.214f)
    near("anchor B range", p.ranges["ANCHOR-B"]!!, 7.882f)
    near("CIR amplitude", p.cirAmplitude, 0.42f)
    near("CIR phase", p.cirPhase, 1.87f)
    check("A is line of sight", p.lineOfSight["ANCHOR-A"] == true)
    check("B is line of sight", p.lineOfSight["ANCHOR-B"] == true)

    section("parsing: NLOS marker")
    val nlos = UwbGeometry.parseLine("ANCHOR-A=3.2*,ANCHOR-B=7.9;CIR=0.4,1.0")!!
    check("NLOS anchor still reported", nlos.ranges.size == 2)
    near("NLOS range keeps its value", nlos.ranges["ANCHOR-A"]!!, 3.2f)
    check("NLOS flagged", nlos.lineOfSight["ANCHOR-A"] == false)
    check("LOS not flagged", nlos.lineOfSight["ANCHOR-B"] == true)

    section("parsing: robustness against serial garbage")
    check("empty line -> null", UwbGeometry.parseLine("") == null)
    check("blank line -> null", UwbGeometry.parseLine("   \r\n") == null)
    check("noise -> null", UwbGeometry.parseLine("\u0000\u00ff garbage") == null)
    check("header only -> null", UwbGeometry.parseLine("DWM3000 shell v1.2") == null)
    check("no '=' -> null", UwbGeometry.parseLine("ANCHOR-A") == null)
    check("empty value -> null", UwbGeometry.parseLine("ANCHOR-A=") == null)
    check("non-numeric -> null", UwbGeometry.parseLine("ANCHOR-A=nan-ish") == null)

    val partial = UwbGeometry.parseLine("ANCHOR-A=1.5,ANCHOR-B=oops,ANCHOR-C=2.5")!!
    check("bad token skipped, good ones kept", partial.ranges.keys == setOf("ANCHOR-A", "ANCHOR-C"))

    check("negative range rejected", UwbGeometry.parseLine("ANCHOR-A=-3.0") == null)
    check("absurd range rejected", UwbGeometry.parseLine("ANCHOR-A=99999.0") == null)

    val noCir = UwbGeometry.parseLine("ANCHOR-A=4.0")!!
    near("range without CIR", noCir.ranges["ANCHOR-A"]!!, 4.0f)
    near("absent CIR is zero", noCir.cirAmplitude, 0.0f)

    val cirOnly = UwbGeometry.parseLine(";CIR=0.9,0.1")!!
    check("CIR-only line kept (through-wall sensing needs it)", cirOnly.ranges.isEmpty())
    near("CIR-only amplitude", cirOnly.cirAmplitude, 0.9f)

    val spaced = UwbGeometry.parseLine("  ANCHOR-A = 3.0 , ANCHOR-B = 4.0 ; CIR=0.1,0.2")!!
    check("whitespace tolerated", spaced.ranges.size == 2)

    val truncated = UwbGeometry.parseLine("ANCHOR-A=3.0;CIR=0.5")!!
    near("truncated CIR keeps amplitude", truncated.cirAmplitude, 0.5f)
    near("truncated CIR phase defaults to 0", truncated.cirPhase, 0.0f)

    // ---------------------------------------------------------- trilateration
    section("trilateration: exact fix")
    val anchors = mapOf(
        "A" to floatArrayOf(0f, 0f, 0f),
        "B" to floatArrayOf(10f, 0f, 0f),
        "C" to floatArrayOf(0f, 10f, 0f),
        "D" to floatArrayOf(10f, 10f, 0f),
    )
    val truth = floatArrayOf(3.0f, 4.0f)
    fun rangeTo(a: FloatArray) = hypot(truth[0] - a[0], truth[1] - a[1])
    val clean = anchors.mapValues { (_, a) -> rangeTo(a) }

    val fix = UwbGeometry.trilaterate(anchors, clean)
    check("four clean anchors produce a fix", fix != null)
    fix!!
    near("x recovered", fix[0], truth[0], 1e-2f)
    near("y recovered", fix[1], truth[1], 1e-2f)

    section("trilateration: minimum anchor count")
    check("zero anchors -> null", UwbGeometry.trilaterate(anchors, emptyMap()) == null)
    check("one anchor -> null", UwbGeometry.trilaterate(anchors, mapOf("A" to 5f)) == null)
    check(
        "two anchors -> null (the solution is a point pair, not a fix)",
        UwbGeometry.trilaterate(anchors, mapOf("A" to 5f, "B" to 5f)) == null,
    )
    check("exactly three anchors -> fix", UwbGeometry.trilaterate(anchors, clean.filterKeys { it != "D" }) != null)

    section("trilateration: unknown anchors are ignored, not fatal")
    val withGhost = clean + ("GHOST" to 5.0f)
    val ghostFix = UwbGeometry.trilaterate(anchors, withGhost)
    check("unconfigured anchor ignored", ghostFix != null)
    near("ghost does not shift x", ghostFix!![0], truth[0], 1e-2f)
    check(
        "three known + one unknown still solves",
        UwbGeometry.trilaterate(anchors, mapOf("A" to clean["A"]!!, "B" to clean["B"]!!, "C" to clean["C"]!!, "GHOST" to 1f)) != null,
    )
    check(
        "two known + one unknown does NOT solve",
        UwbGeometry.trilaterate(anchors, mapOf("A" to clean["A"]!!, "B" to clean["B"]!!, "GHOST" to 1f)) == null,
    )

    section("trilateration: degenerate geometry is refused")
    val collinear = mapOf(
        "A" to floatArrayOf(0f, 0f, 0f),
        "B" to floatArrayOf(5f, 0f, 0f),
        "C" to floatArrayOf(10f, 0f, 0f),
    )
    check(
        "collinear anchors -> null (position mirrors across the baseline)",
        UwbGeometry.trilaterate(collinear, mapOf("A" to 5f, "B" to 4.0f, "C" to 5f)) == null,
    )
    val coincident = mapOf(
        "A" to floatArrayOf(1f, 1f, 0f),
        "B" to floatArrayOf(1f, 1f, 0f),
        "C" to floatArrayOf(1f, 1f, 0f),
    )
    check(
        "coincident anchors -> null",
        UwbGeometry.trilaterate(coincident, mapOf("A" to 2f, "B" to 2f, "C" to 2f)) == null,
    )
    // The dangerous case: a triangle that is *technically* non-degenerate but
    // so thin that noise is amplified enormously. Its determinant and its
    // determinant-to-scale ratio both look healthy (~1e5 and 0.20), so only a
    // conditioning test catches it. Measured: 1 cm of range noise here moves
    // the fix by ~833 m.
    val sliver = mapOf(
        "A" to floatArrayOf(0f, 0f, 0f),
        "B" to floatArrayOf(30f, 0f, 0f),
        "C" to floatArrayOf(60f, 0.001f, 0f),
    )
    val sliverTruth = floatArrayOf(20f, 5f)
    val sliverRanges = sliver.mapValues { (_, a) -> hypot(sliverTruth[0] - a[0], sliverTruth[1] - a[1]) }
    check(
        "near-collinear sliver -> null (healthy determinant, catastrophic conditioning)",
        UwbGeometry.trilaterate(sliver, sliverRanges) == null,
    )
    val sliver2 = mapOf(
        "A" to floatArrayOf(0f, 0f, 0f),
        "B" to floatArrayOf(30f, 0f, 0f),
        "C" to floatArrayOf(60f, 0.05f, 0f),
    )
    check(
        "60m x 5cm sliver -> null",
        UwbGeometry.trilaterate(sliver2, sliver2.mapValues { (_, a) -> hypot(20f - a[0], 5f - a[1]) }) == null,
    )
    // A fixed determinant threshold would wrongly accept this: the anchors are
    // collinear, but scaled down so far that |det| is tiny for a *good* rig too.
    val tinyCollinear = mapOf(
        "A" to floatArrayOf(0f, 0f, 0f),
        "B" to floatArrayOf(0.05f, 0f, 0f),
        "C" to floatArrayOf(0.10f, 0f, 0f),
    )
    check(
        "small-scale collinear rig -> null (scale-aware test)",
        UwbGeometry.trilaterate(tinyCollinear, mapOf("A" to 0.5f, "B" to 0.48f, "C" to 0.5f)) == null,
    )
    // ...while a genuinely small but well-conditioned rig must still solve.
    val tinyValid = mapOf(
        "A" to floatArrayOf(0f, 0f, 0f),
        "B" to floatArrayOf(0.20f, 0f, 0f),
        "C" to floatArrayOf(0f, 0.20f, 0f),
    )
    val tinyTruth = floatArrayOf(0.06f, 0.08f)
    val tinyRanges = tinyValid.mapValues { (_, a) -> hypot(tinyTruth[0] - a[0], tinyTruth[1] - a[1]) }
    val tinyFix = UwbGeometry.trilaterate(tinyValid, tinyRanges)
    check("small but well-conditioned rig still solves", tinyFix != null)
    near("small-rig x", tinyFix!![0], tinyTruth[0], 5e-3f)
    near("small-rig y", tinyFix[1], tinyTruth[1], 5e-3f)

    section("trilateration: well-shaped rigs are never rejected")
    // Guard against the conditioning test being tightened into uselessness.
    val equilateral = mapOf(
        "A" to floatArrayOf(0f, 0f, 0f),
        "B" to floatArrayOf(5f, 0f, 0f),
        "C" to floatArrayOf(2.5f, 4.33f, 0f),
    )
    val eqTruth = floatArrayOf(2.0f, 1.5f)
    val eqFix = UwbGeometry.trilaterate(
        equilateral,
        equilateral.mapValues { (_, a) -> hypot(eqTruth[0] - a[0], eqTruth[1] - a[1]) },
    )
    check("equilateral rig accepted", eqFix != null)
    near("equilateral x", eqFix!![0], eqTruth[0], 1e-2f)
    near("equilateral y", eqFix[1], eqTruth[1], 1e-2f)

    section("trilateration: precision at survey distances")
    // Anchors far from the origin are where Float intermediates lose their
    // significant digits: the b term differences squared coordinates.
    val far = mapOf(
        "A" to floatArrayOf(1000f, 1000f, 0f),
        "B" to floatArrayOf(1030f, 1000f, 0f),
        "C" to floatArrayOf(1000f, 1030f, 0f),
        "D" to floatArrayOf(1030f, 1030f, 0f),
    )
    val farTruth = floatArrayOf(1012.0f, 1017.0f)
    val farRanges = far.mapValues { (_, a) -> hypot(farTruth[0] - a[0], farTruth[1] - a[1]) }
    val farFix = UwbGeometry.trilaterate(far, farRanges)
    check("distant rig solves", farFix != null)
    near("distant x within a centimetre", farFix!![0], farTruth[0], 0.01f)
    near("distant y within a centimetre", farFix[1], farTruth[1], 0.01f)

    section("trilateration: noise degrades gracefully")
    // Least squares over four anchors should average out symmetric range noise
    // rather than amplify it.
    val noisy = mapOf(
        "A" to clean["A"]!! + 0.05f,
        "B" to clean["B"]!! - 0.05f,
        "C" to clean["C"]!! + 0.05f,
        "D" to clean["D"]!! - 0.05f,
    )
    val noisyFix = UwbGeometry.trilaterate(anchors, noisy)!!
    val err = hypot(noisyFix[0] - truth[0], noisyFix[1] - truth[1])
    check("5 cm range noise stays under 20 cm of position error (got $err)", err < 0.20f)

    // ------------------------------------------------------------------ done
    println("\n" + "-".repeat(40))
    println("$checks checks, $failures failures")
    if (failures > 0) kotlin.system.exitProcess(1)
}
