import com.aura.agent.fusion.PositionQuality

/**
 * The quality tiers must agree with `classify_quality` in the Python agent.
 *
 * The handheld and the command post describe the same estimate to different
 * people at the same moment. If they disagree about whether a position is
 * usable, one of them is lying to somebody who is acting on it.
 *
 * Expected values are generated from the Python reference by
 * tools/run-kotlin-tests.sh and substituted in as @CASES@.
 */

private var checks = 0
private var failures = 0

private fun check(name: String, cond: Boolean) {
    checks++
    if (!cond) { failures++; println("  FAIL: $name") }
}

fun main() {
    println("\n== tier boundaries ==")
    check("0.0 -> GOOD", PositionQuality.forSigma(0.0f) == PositionQuality.GOOD)
    check("0.75 -> GOOD (inclusive)", PositionQuality.forSigma(0.75f) == PositionQuality.GOOD)
    check("0.7501 -> DEGRADED", PositionQuality.forSigma(0.7501f) == PositionQuality.DEGRADED)
    check("3.0 -> DEGRADED (inclusive)", PositionQuality.forSigma(3.0f) == PositionQuality.DEGRADED)
    check("3.0001 -> POOR", PositionQuality.forSigma(3.0001f) == PositionQuality.POOR)
    check("10.0 -> POOR (inclusive)", PositionQuality.forSigma(10.0f) == PositionQuality.POOR)
    check("10.0001 -> LOST", PositionQuality.forSigma(10.0001f) == PositionQuality.LOST)

    println("\n== the measured drift cases ==")
    // These are the numbers from docs/open_issues_research.md. Each one used to
    // render identically to a healthy 0.8 m fix.
    // sigma values measured after each outage, and the tier they must produce.
    check("sigma 6.59 (10 s outage) -> POOR", PositionQuality.forSigma(6.59f) == PositionQuality.POOR)
    check("sigma 93.2 (30 s outage) -> LOST", PositionQuality.forSigma(93.24f) == PositionQuality.LOST)
    check("sigma 618 (60 s standing) -> LOST", PositionQuality.forSigma(618.09f) == PositionQuality.LOST)
    check("sigma 589 (60 s walking) -> LOST", PositionQuality.forSigma(589.11f) == PositionQuality.LOST)
    // The one that must still be usable: a good fix during normal operation.
    check("sigma 0.028 (all sensors) -> GOOD", PositionQuality.forSigma(0.028f) == PositionQuality.GOOD)

    println("\n== non-finite input must never read as GOOD ==")
    check("NaN -> LOST", PositionQuality.forSigma(Float.NaN) == PositionQuality.LOST)
    check("+inf -> LOST", PositionQuality.forSigma(Float.POSITIVE_INFINITY) == PositionQuality.LOST)
    // A negative sigma is nonsense, but it must not be reported as unusable
    // either - clamp to the best tier rather than invent a failure.
    check("negative -> GOOD", PositionQuality.forSigma(-1.0f) == PositionQuality.GOOD)

    println("\n== wire names match the Python strings ==")
    check("good", PositionQuality.GOOD.wire == "good")
    check("degraded", PositionQuality.DEGRADED.wire == "degraded")
    check("poor", PositionQuality.POOR.wire == "poor")
    check("lost", PositionQuality.LOST.wire == "lost")

    println("\n== cross-platform: Kotlin must match Python ==")
    // @CASES@ is replaced by the runner with (sigma, expected) pairs produced
    // by the Python classifier itself.
    val cases: List<Pair<Float, String>> = listOf(@CASES@)
    check("fixture list is not empty", cases.isNotEmpty())
    cases.forEach { (sigma, expected) ->
        val got = PositionQuality.forSigma(sigma).wire
        check("sigma=$sigma python=$expected kotlin=$got", got == expected)
    }

    println("\n" + "-".repeat(40))
    println("$checks checks, $failures failures")
    if (failures > 0) kotlin.system.exitProcess(1)
}
