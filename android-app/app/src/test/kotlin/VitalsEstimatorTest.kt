import com.aura.agent.sensors.VitalsEstimator
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.sin
import kotlin.random.Random

/**
 * Host-side tests for the vitals estimator.
 *
 * Signals are synthesised at known rates, so "did it find 15 breaths/min"
 * has an exact answer. The cases that matter most are the negative ones: a
 * false "breathing" readout on an empty room, or a heart rate that is really
 * a respiration harmonic, are the failures with clinical consequences.
 */

private var checks = 0
private var failures = 0

private fun check(name: String, cond: Boolean) {
    checks++
    if (!cond) { failures++; println("  FAIL: $name") }
}

private fun near(name: String, actual: Float?, expected: Float, tol: Float) {
    checks++
    if (actual == null || abs(actual - expected) > tol) {
        failures++
        println("  FAIL: $name (expected $expected +/- $tol, got $actual)")
    }
}

private fun section(t: String) = println("\n== $t ==")

/** Breathing at [respHz], optional heartbeat, optional noise and drift. */
private fun synth(
    seconds: Float,
    rateHz: Float,
    respHz: Float,
    respAmp: Float = 1.0f,
    heartHz: Float = 0f,
    heartAmp: Float = 0f,
    noise: Float = 0f,
    drift: Float = 0f,
    seed: Int = 7,
): FloatArray {
    val rng = Random(seed)
    val n = (seconds * rateHz).toInt()
    return FloatArray(n) { i ->
        val t = i / rateHz
        var v = respAmp * sin(2.0 * PI * respHz * t).toFloat()
        if (heartAmp > 0f) v += heartAmp * sin(2.0 * PI * heartHz * t).toFloat()
        if (drift != 0f) v += drift * t
        if (noise > 0f) v += (rng.nextFloat() - 0.5f) * 2f * noise
        v
    }
}

fun main() {
    val rate = 20f

    section("respiration: clean signal")
    // 15 breaths/min = 0.25 Hz
    var v = VitalsEstimator.estimate(synth(60f, rate, 0.25f), rate)
    check("subject detected", v.hasSubject)
    near("respiration 15 bpm", v.respirationBpm, 15f, 0.6f)

    section("respiration: across the clinical range")
    for ((hz, bpm) in listOf(0.20f to 12f, 0.25f to 15f, 0.33f to 19.8f, 0.50f to 30f)) {
        val r = VitalsEstimator.estimate(synth(60f, rate, hz), rate)
        near("$bpm bpm recovered", r.respirationBpm, bpm, 0.8f)
    }

    section("respiration: survives noise and drift")
    v = VitalsEstimator.estimate(synth(60f, rate, 0.25f, respAmp = 1f, noise = 0.5f, drift = 0.05f), rate)
    check("still detected with noise + drift", v.hasSubject)
    near("rate still correct", v.respirationBpm, 15f, 1.0f)

    // Linear drift is far larger than the signal - the detrend must handle it.
    v = VitalsEstimator.estimate(synth(60f, rate, 0.25f, respAmp = 1f, drift = 2.0f), rate)
    near("heavy drift removed", v.respirationBpm, 15f, 1.0f)

    section("empty room must NOT report a subject")
    // Pure noise: any "respiration" here is a false positive on a casualty search.
    var falsePositives = 0
    for (seed in 1..40) {
        val noiseOnly = FloatArray(1200) { (Random(seed).nextFloat() - 0.5f) * 2f }
        if (VitalsEstimator.estimate(noiseOnly, rate).hasSubject) falsePositives++
    }
    check("no false positives on 40 noise-only windows (got $falsePositives)", falsePositives == 0)

    val flat = FloatArray(1200) { 0.5f }
    check("flat signal reports nothing", !VitalsEstimator.estimate(flat, rate).hasSubject)

    val ramp = FloatArray(1200) { it * 0.01f }
    check("pure drift reports nothing", !VitalsEstimator.estimate(ramp, rate).hasSubject)

    section("heart rate")
    // 0.25 Hz breathing (15/min) + 1.15 Hz heartbeat (69 bpm), well separated
    // from 0.25's harmonics at 0.50/0.75/1.00/1.25.
    v = VitalsEstimator.estimate(
        synth(60f, rate, 0.25f, respAmp = 1.0f, heartHz = 1.15f, heartAmp = 0.09f), rate,
    )
    near("respiration still 15 bpm", v.respirationBpm, 15f, 0.8f)
    near("heart rate 69 bpm", v.heartRateBpm, 69f, 3f)

    section("a respiration harmonic must NOT be reported as a pulse")
    // Breathing at 0.30 Hz with a strong 3rd harmonic at 0.90 Hz - inside the
    // cardiac band. Reporting 54 bpm here would be a clinically dangerous lie.
    val n = (60f * rate).toInt()
    val harmonic = FloatArray(n) { i ->
        val t = i / rate
        (sin(2.0 * PI * 0.30 * t) + 0.35 * sin(2.0 * PI * 0.90 * t)).toFloat()
    }
    v = VitalsEstimator.estimate(harmonic, rate)
    near("respiration found", v.respirationBpm, 18f, 1.0f)
    check("harmonic rejected as heart rate (got ${v.heartRateBpm})", v.heartRateBpm == null)

    section("a real pulse near a strong harmonic must still be found")
    // The hard case, and the clinically important one: non-sinusoidal chest
    // motion (so harmonics are genuinely strong) PLUS a real pulse at 1.15 Hz,
    // which sits 8% away from the 5th harmonic of 0.25 Hz. A guard that keys
    // on frequency proximity alone suppresses this pulse and reports a
    // breathing casualty as having no heartbeat.
    val nearHarmonic = FloatArray(n) { i ->
        val t = i / rate
        (sin(2.0 * PI * 0.25 * t) +
            0.35 * sin(2.0 * PI * 0.50 * t) +
            0.20 * sin(2.0 * PI * 0.75 * t) +
            0.10 * sin(2.0 * PI * 1.25 * t) +
            0.09 * sin(2.0 * PI * 1.15 * t)).toFloat()
    }
    v = VitalsEstimator.estimate(nearHarmonic, rate)
    near("respiration found alongside harmonics", v.respirationBpm, 15f, 0.8f)
    near("real pulse at 69 bpm not suppressed by the nearby harmonic", v.heartRateBpm, 69f, 3f)

    section("windowing must not be skipped")
    // Without a Hann window, spectral leakage from the strong respiration
    // component floods the cardiac band. Use a rate that is not an exact bin
    // so leakage is at its worst.
    val leaky = FloatArray(n) { i ->
        val t = i / rate
        (sin(2.0 * PI * 0.2333 * t) + 0.05 * sin(2.0 * PI * 1.1 * t)).toFloat()
    }
    v = VitalsEstimator.estimate(leaky, rate)
    near("off-bin respiration still resolved", v.respirationBpm, 14f, 1.2f)

    section("short windows are refused rather than guessed at")
    check("empty input", !VitalsEstimator.estimate(FloatArray(0), rate).hasSubject)
    check("below MIN_SAMPLES", !VitalsEstimator.estimate(FloatArray(32) { sin(it * 0.1).toFloat() }, rate).hasSubject)
    check("zero sample rate", !VitalsEstimator.estimate(FloatArray(256), 0f).hasSubject)
    check("negative sample rate", !VitalsEstimator.estimate(FloatArray(256), -5f).hasSubject)

    section("building blocks")
    val det = VitalsEstimator.detrend(floatArrayOf(1f, 2f, 3f, 4f, 5f))
    check("perfect ramp detrends to ~zero", det.all { abs(it) < 1e-4f })

    val tone = FloatArray(200) { sin(2.0 * PI * 2.0 * it / 20.0).toFloat() }
    val onFreq = VitalsEstimator.goertzelPower(tone, 20f, 2.0f)
    val offFreq = VitalsEstimator.goertzelPower(tone, 20f, 5.0f)
    check("Goertzel peaks at the true frequency ($onFreq vs $offFreq)", onFreq > offFreq * 10f)

    val win = FloatArray(64) { 1f }
    VitalsEstimator.applyHann(win)
    check("Hann tapers to zero at the edges", win.first() < 1e-6f && win.last() < 1e-6f)
    check("Hann peaks in the middle", win[32] > 0.99f)

    println("\n" + "-".repeat(40))
    println("$checks checks, $failures failures")
    if (failures > 0) kotlin.system.exitProcess(1)
}
