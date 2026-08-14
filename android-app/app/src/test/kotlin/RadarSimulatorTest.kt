import com.aura.agent.radar.RadarSimulator

/**
 * Host-side tests for the passive-radar IQ simulator.
 *
 * What is being pinned: the *conventions* the device path depends on. The
 * simulator delays the echo by `round(range · fs / c)` samples because that
 * is how the native CAF maps lag to range (`bin · c / fs`, same as
 * `passive_radar.py`); if that mapping ever drifts, the synthetic scene would
 * place a target on a different bin than the operator reads — a failure that
 * looks like a working radar and is invisible until real hardware arrives.
 *
 * The native CAF itself is covered by `test_aura_core.cpp`; this suite only
 * checks that the injected scene has the delay and Doppler it claims.
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

/** Real part of the correlation of surveillance with reference at [lag]. */
private fun correlate(surv: FloatArray, ref: FloatArray, n: Int, lag: Int): Double {
    var acc = 0.0
    for (i in lag until n) {
        acc += surv[2 * i].toDouble() * ref[2 * (i - lag)] +
            surv[2 * i + 1].toDouble() * ref[2 * (i - lag) + 1]
    }
    return acc
}

/** DFT magnitude at bin [k] of a complex sequence (parallel re/im arrays). */
private fun dftBin(reSeq: DoubleArray, imSeq: DoubleArray, n: Int, k: Int): Double {
    var re = 0.0
    var im = 0.0
    for (i in 0 until n) {
        val angle = -2.0 * Math.PI * k * i / n
        val c = Math.cos(angle)
        val s = Math.sin(angle)
        re += reSeq[i] * c - imSeq[i] * s
        im += reSeq[i] * s + imSeq[i] * c
    }
    return Math.sqrt(re * re + im * im)
}

fun main() {
    println("AURA 6.0 - radar simulator suite")

    // --- lag <-> range convention --------------------------------------
    val sim = RadarSimulator()
    for (k in 1..10) {
        check(
            sim.delaySamples(sim.rangeMetres(k)) == k,
            "rangeMetres(delaySamples(k)) round-trips for k=$k",
        )
    }
    // c / 2.4e6 = 124.9 m per lag: the doc in NativePassiveRadar says
    // c/(2B) is the *resolution*; the CAF's lag axis is one bin per c/fs.
    check(
        kotlin.math.abs(sim.rangeMetres(1) - 299_792_458.0f / 2.4e6f) < 0.01f,
        "lag 1 reads as c/fs metres",
    )
    check(sim.delaySamples(124.913f) == 1, "target near one lag bin lands on lag 1")

    // --- dwell geometry ------------------------------------------------
    val dwell = sim.dwell(durationMs = 5.0f, target = null)
    val n = dwell.sampleCount
    check(n == (2.4e6 * 0.005).toInt(), "dwell length matches duration x fs")
    check(dwell.surveillance.size == 2 * n && dwell.reference.size == 2 * n,
        "channels are 2*n interleaved floats")
    check(
        sim.dwell(durationMs = 1.0f).sampleCount == (2.4e6 * 0.001).toInt(),
        "duration scales the sample count",
    )

    // --- determinism: same seed, same scene ----------------------------
    val a = RadarSimulator(seed = 7).dwell(durationMs = 2.0f, target = null)
    val b = RadarSimulator(seed = 7).dwell(durationMs = 2.0f, target = null)
    check(a.reference.contentEquals(b.reference), "same seed reproduces the reference")
    check(a.surveillance.contentEquals(b.surveillance), "same seed reproduces the surveillance")

    // --- correlation peak at the injected lag --------------------------
    val fs = 2.4e6f
    val sim2 = RadarSimulator(sampleRate = fs)
    val lag = 4
    val range = sim2.rangeMetres(lag)
    val scene = sim2.dwell(
        durationMs = 5.0f,
        target = RadarSimulator.TargetSpec(range, dopplerHz = 0f, amplitude = 0.08f),
        noiseSigma = 0.02f,
    )
    val n2 = scene.sampleCount
    val corr = DoubleArray(13)
    for (k in 0..12) corr[k] = correlate(scene.surveillance, scene.reference, n2, k)
    check(corr[0] > 0.0, "direct path dominates lag 0 (real correlation)")
    // Lag 0 is the direct-path clutter the ECA canceller removes; the target
    // echo must be the strongest peak among the non-zero lags.
    val peak = (1..12).maxByOrNull { corr[it] } ?: -1
    check(peak == lag, "correlation peaks at the injected lag $lag, got $peak")
    check(
        corr[lag] > 5.0 * corr[(lag + 1).coerceAtMost(12)].coerceAtLeast(1e-9) &&
            corr[lag] > 5.0 * corr[(lag - 1).coerceAtLeast(1)].coerceAtLeast(1e-9),
        "lag peak is well above its neighbours",
    )

    // --- Doppler rotation appears at the injected frequency -------------
    // fs=128, n=512 -> a target at 8 Hz lands exactly on DFT bin 32, so the
    // test asserts the rotation rate without interpolation. (The range that
    // goes with a 3-lag delay at this fs is physically absurd; this test is
    // about the modulation math, not a plausible scene.)
    val fsD = 128.0f
    val simD = RadarSimulator(sampleRate = fsD)
    val nD = simD.dwell(durationMs = 4000f, target = null).sampleCount
    check(nD == 512, "Doppler scene has 512 samples")
    val dopplerHz = 8.0f
    // No direct path: this scene models the surveillance channel *after* ECA
    // cancellation, so the only lag-3 contribution is the Doppler-rotated
    // echo and the rotation rate is the quantity under test.
    val dopplerScene = simD.dwell(
        durationMs = 4000f,
        target = RadarSimulator.TargetSpec(simD.rangeMetres(3), dopplerHz = dopplerHz, amplitude = 0.1f),
        directPathGain = 0.0f,
        noiseSigma = 0.0f,
    )
    // Complex product of surveillance with the conjugate reference at the true
    // delay: the echo contributes |ref|^2 * e^{j*2*pi*fd*t}. Its complex DFT
    // coheres at fd (bin 32) because the rotation exactly cancels the bin
    // twiddle factor; every other bin is an incoherent random walk.
    val prodRe = DoubleArray(nD)
    val prodIm = DoubleArray(nD)
    for (i in 3 until nD) {
        // (x + jy) * conj(a + jb) = (xa + yb) + j(ya - xb)
        val x = dopplerScene.surveillance[2 * i].toDouble()
        val y = dopplerScene.surveillance[2 * i + 1].toDouble()
        val a = dopplerScene.reference[2 * (i - 3)].toDouble()
        val b = dopplerScene.reference[2 * (i - 3) + 1].toDouble()
        prodRe[i] = x * a + y * b
        prodIm[i] = y * a - x * b
    }
    val bin32 = dftBin(prodRe, prodIm, nD, 32)
    val offBins = intArrayOf(8, 16, 24, 40, 48, 56, 64).map { dftBin(prodRe, prodIm, nD, it) }
    check(bin32 > 3.0 * (offBins.maxOrNull() ?: 1e-9),
        "Doppler energy sits at bin 32 (8 Hz), not in the neighbouring bins")

    // --- clutter-only scene has no off-zero peak ------------------------
    val clean = sim2.dwell(durationMs = 5.0f, target = null, noiseSigma = 0.0f)
    val cleanCorr = DoubleArray(13) { correlate(clean.surveillance, clean.reference, n2, it) }
    check(cleanCorr[0] == cleanCorr[0] && cleanCorr[0] > 0.0, "direct path present")
    val cleanPeak = cleanCorr.indices.maxByOrNull { cleanCorr[it] } ?: -1
    check(cleanPeak == 0, "no target -> peak stays at lag 0, got $cleanPeak")

    println("\n----------------------------------------")
    println("$checks checks, $failures failures")
    if (failures > 0) kotlin.system.exitProcess(1)
}
