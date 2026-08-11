package com.swarmradar.app.domain.alignment

import com.swarmradar.app.domain.model.RigidTransform
import com.swarmradar.app.domain.model.Vector3
import org.ejml.simple.SimpleMatrix
import javax.inject.Inject
import javax.inject.Singleton

/**
 * Umeyama-Algorithmus: optimale starre Transformation zwischen zwei Punktmengen
 * (Least-Squares, inkl. Spiegelungs-Korrektur).
 */
@Singleton
class UmeyamaAlignment @Inject constructor() {

    fun align(source: List<Vector3>, target: List<Vector3>): RigidTransform {
        require(source.size == target.size && source.isNotEmpty())

        val n = source.size.toDouble()
        val srcMean = source.reduce { a, b -> a + b } / n
        val tgtMean = target.reduce { a, b -> a + b } / n

        val H = SimpleMatrix(3, 3)
        for (i in source.indices) {
            val s = (source[i] - srcMean).toCol()
            val t = (target[i] - tgtMean).toCol()
            H.plusEquals(s.mult(t.transpose()))
        }

        val svd = H.svd()
        var R = svd.v.mult(svd.u.transpose())
        if (R.determinant() < 0.0) {
            R = R.negative()
        }
        val t = (tgtMean - R.mult(srcMean.toCol())).toVec()
        return RigidTransform(R, t)
    }

    private fun Vector3.toCol(): SimpleMatrix {
        val m = SimpleMatrix(3, 1)
        m[0] = x; m[1] = y; m[2] = z
        return m
    }

    private fun SimpleMatrix.toVec(): Vector3 =
        Vector3(get(0), get(1), get(2))
}
