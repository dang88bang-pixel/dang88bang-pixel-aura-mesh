// aura_core.cpp - implementation of the AURA 6.0 native core.
// See aura_core.h for the porting contract against the Python reference.

#include "aura_core.h"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <limits>

#if defined(__ARM_NEON) || defined(__ARM_NEON__)
#include <arm_neon.h>
#define AURA_HAVE_NEON 1
#endif

namespace aura {

namespace {
constexpr float kPi = 3.14159265358979323846f;
constexpr float kGravity = 9.80665f;
constexpr float kSpeedOfLight = 299792458.0f;
}  // namespace

// =====================================================================
// linear algebra
// =====================================================================

float dot(const float* a, const float* b, int n) {
#ifdef AURA_HAVE_NEON
    float32x4_t acc = vdupq_n_f32(0.0f);
    int i = 0;
    for (; i + 4 <= n; i += 4) {
        acc = vmlaq_f32(acc, vld1q_f32(a + i), vld1q_f32(b + i));
    }
    float sum = vgetq_lane_f32(acc, 0) + vgetq_lane_f32(acc, 1) +
                vgetq_lane_f32(acc, 2) + vgetq_lane_f32(acc, 3);
    for (; i < n; ++i) sum += a[i] * b[i];
    return sum;
#else
    // Pairwise-ish accumulation in double keeps the reference and the NEON
    // path within float tolerance on long vectors.
    double sum = 0.0;
    for (int i = 0; i < n; ++i) sum += static_cast<double>(a[i]) * b[i];
    return static_cast<float>(sum);
#endif
}

float norm2(const float* a, int n) { return std::sqrt(dot(a, a, n)); }

void matVec(const float* A, const float* x, float* y, int rows, int cols) {
    for (int r = 0; r < rows; ++r) {
        y[r] = dot(A + static_cast<size_t>(r) * cols, x, cols);
    }
}

void matTVec(const float* A, const float* x, float* y, int rows, int cols) {
    std::memset(y, 0, sizeof(float) * static_cast<size_t>(cols));
    for (int r = 0; r < rows; ++r) {
        const float scale = x[r];
        if (scale == 0.0f) continue;
        const float* row = A + static_cast<size_t>(r) * cols;
#ifdef AURA_HAVE_NEON
        const float32x4_t vs = vdupq_n_f32(scale);
        int c = 0;
        for (; c + 4 <= cols; c += 4) {
            vst1q_f32(y + c, vmlaq_f32(vld1q_f32(y + c), vld1q_f32(row + c), vs));
        }
        for (; c < cols; ++c) y[c] += row[c] * scale;
#else
        for (int c = 0; c < cols; ++c) y[c] += row[c] * scale;
#endif
    }
}

// =====================================================================
// RTI
// =====================================================================

float powerIterationLipschitz(const float* W, int rows, int cols, int iterations) {
    if (rows <= 0 || cols <= 0) return 1.0f;
    std::vector<float> v(cols), tmp(rows), w(cols);
    // deterministic start vector (matches the Python default_rng(0) behaviour
    // closely enough - power iteration converges from any non-orthogonal seed)
    for (int i = 0; i < cols; ++i) v[i] = 1.0f / std::sqrt(static_cast<float>(cols));

    float eigenvalue = 1.0f;
    for (int it = 0; it < iterations; ++it) {
        matVec(W, v.data(), tmp.data(), rows, cols);
        matTVec(W, tmp.data(), w.data(), rows, cols);
        const float n = norm2(w.data(), cols);
        if (n < 1e-18f) return 1.0f;
        for (int i = 0; i < cols; ++i) v[i] = w[i] / n;
        eigenvalue = n;
    }
    return std::max(eigenvalue, 1e-9f);
}

void softThreshold(float* x, int n, float threshold, bool nonNegative) {
#ifdef AURA_HAVE_NEON
    const float32x4_t vt = vdupq_n_f32(threshold);
    const float32x4_t zero = vdupq_n_f32(0.0f);
    int i = 0;
    for (; i + 4 <= n; i += 4) {
        float32x4_t v = vld1q_f32(x + i);
        float32x4_t mag = vsubq_f32(vabsq_f32(v), vt);
        mag = vmaxq_f32(mag, zero);
        // copy the sign of v onto mag
        uint32x4_t sign = vandq_u32(vreinterpretq_u32_f32(v), vdupq_n_u32(0x80000000u));
        float32x4_t out = vreinterpretq_f32_u32(vorrq_u32(vreinterpretq_u32_f32(mag), sign));
        if (nonNegative) out = vmaxq_f32(out, zero);
        vst1q_f32(x + i, out);
    }
    for (; i < n; ++i) {
        const float mag = std::max(std::fabs(x[i]) - threshold, 0.0f);
        x[i] = std::copysign(mag, x[i]);
        if (nonNegative && x[i] < 0.0f) x[i] = 0.0f;
    }
#else
    for (int i = 0; i < n; ++i) {
        const float mag = std::max(std::fabs(x[i]) - threshold, 0.0f);
        x[i] = std::copysign(mag, x[i]);
        if (nonNegative && x[i] < 0.0f) x[i] = 0.0f;
    }
#endif
}

FistaResult reconstructL1Fista(const float* W, const float* y, int rows, int cols,
                               float alpha, int maxIter, float tolerance,
                               bool nonNegative, float lipschitz) {
    FistaResult result;
    result.image.assign(static_cast<size_t>(cols), 0.0f);
    if (rows <= 0 || cols <= 0) return result;

    const float L = lipschitz > 0.0f ? lipschitz
                                     : powerIterationLipschitz(W, rows, cols);
    result.lipschitz = L;

    std::vector<float> x(cols, 0.0f), z(cols, 0.0f), xNew(cols, 0.0f);
    std::vector<float> residual(rows, 0.0f), grad(cols, 0.0f);
    float t = 1.0f;
    int iteration = 0;

    for (iteration = 1; iteration <= maxIter; ++iteration) {
        // grad = W^T (W z - y)
        matVec(W, z.data(), residual.data(), rows, cols);
        for (int r = 0; r < rows; ++r) residual[r] -= y[r];
        matTVec(W, residual.data(), grad.data(), rows, cols);

        for (int c = 0; c < cols; ++c) xNew[c] = z[c] - grad[c] / L;
        softThreshold(xNew.data(), cols, alpha / L, nonNegative);

        const float tNew = 0.5f * (1.0f + std::sqrt(1.0f + 4.0f * t * t));
        const float momentum = (t - 1.0f) / tNew;
        for (int c = 0; c < cols; ++c) {
            z[c] = xNew[c] + momentum * (xNew[c] - x[c]);
        }

        float deltaNorm = 0.0f, xNorm = 0.0f;
        for (int c = 0; c < cols; ++c) {
            const float d = xNew[c] - x[c];
            deltaNorm += d * d;
            xNorm += xNew[c] * xNew[c];
        }
        std::copy(xNew.begin(), xNew.end(), x.begin());
        t = tNew;

        const float rel = std::sqrt(deltaNorm) / std::max(1e-12f, std::sqrt(xNorm));
        if (rel < tolerance) {
            result.converged = true;
            break;
        }
    }

    matVec(W, x.data(), residual.data(), rows, cols);
    float dataTerm = 0.0f, l1 = 0.0f;
    for (int r = 0; r < rows; ++r) {
        const float d = residual[r] - y[r];
        dataTerm += d * d;
    }
    for (int c = 0; c < cols; ++c) l1 += std::fabs(x[c]);

    result.image = x;
    result.iterations = std::min(iteration, maxIter);
    result.objective = 0.5f * dataTerm + alpha * l1;
    return result;
}

FloatVec reconstructTikhonov(const float* W, const float* y, int rows, int cols,
                             float alpha) {
    // A = W^T W + alpha I  (cols x cols, symmetric positive definite)
    std::vector<float> A(static_cast<size_t>(cols) * cols, 0.0f);
    for (int i = 0; i < cols; ++i) {
        for (int j = i; j < cols; ++j) {
            double acc = 0.0;
            for (int r = 0; r < rows; ++r) {
                acc += static_cast<double>(W[static_cast<size_t>(r) * cols + i]) *
                       W[static_cast<size_t>(r) * cols + j];
            }
            float value = static_cast<float>(acc);
            if (i == j) value += alpha;
            A[static_cast<size_t>(i) * cols + j] = value;
            A[static_cast<size_t>(j) * cols + i] = value;
        }
    }
    std::vector<float> b(cols, 0.0f);
    matTVec(W, y, b.data(), rows, cols);

    // Cholesky: A = L L^T
    std::vector<float> L(static_cast<size_t>(cols) * cols, 0.0f);
    for (int i = 0; i < cols; ++i) {
        for (int j = 0; j <= i; ++j) {
            double sum = A[static_cast<size_t>(i) * cols + j];
            for (int k = 0; k < j; ++k) {
                sum -= static_cast<double>(L[static_cast<size_t>(i) * cols + k]) *
                       L[static_cast<size_t>(j) * cols + k];
            }
            if (i == j) {
                L[static_cast<size_t>(i) * cols + j] =
                    std::sqrt(std::max(static_cast<float>(sum), 1e-12f));
            } else {
                L[static_cast<size_t>(i) * cols + j] =
                    static_cast<float>(sum) / L[static_cast<size_t>(j) * cols + j];
            }
        }
    }
    // forward / back substitution
    std::vector<float> tmp(cols, 0.0f), out(cols, 0.0f);
    for (int i = 0; i < cols; ++i) {
        double sum = b[i];
        for (int k = 0; k < i; ++k) sum -= L[static_cast<size_t>(i) * cols + k] * tmp[k];
        tmp[i] = static_cast<float>(sum) / L[static_cast<size_t>(i) * cols + i];
    }
    for (int i = cols - 1; i >= 0; --i) {
        double sum = tmp[i];
        for (int k = i + 1; k < cols; ++k) sum -= L[static_cast<size_t>(k) * cols + i] * out[k];
        out[i] = static_cast<float>(sum) / L[static_cast<size_t>(i) * cols + i];
    }
    return out;
}

std::vector<float> buildWeightMatrix(const float* nodes, int nodeCount,
                                     float minX, float minY, float resolution,
                                     int nx, int ny, float ellipseWidth) {
    const int links = nodeCount * (nodeCount - 1) / 2;
    const int voxels = nx * ny;
    std::vector<float> W(static_cast<size_t>(links) * voxels, 0.0f);

    int row = 0;
    for (int a = 0; a < nodeCount; ++a) {
        for (int b = a + 1; b < nodeCount; ++b, ++row) {
            const float ax = nodes[2 * a], ay = nodes[2 * a + 1];
            const float bx = nodes[2 * b], by = nodes[2 * b + 1];
            const float length = std::hypot(bx - ax, by - ay);
            if (length < 1e-6f) continue;
            const float invSqrt = 1.0f / std::sqrt(length);
            float* out = W.data() + static_cast<size_t>(row) * voxels;
            for (int j = 0; j < ny; ++j) {
                const float vy = minY + (j + 0.5f) * resolution;
                for (int i = 0; i < nx; ++i) {
                    const float vx = minX + (i + 0.5f) * resolution;
                    const float da = std::hypot(vx - ax, vy - ay);
                    const float db = std::hypot(vx - bx, vy - by);
                    if (da + db - length < ellipseWidth) {
                        out[j * nx + i] = invSqrt;
                    }
                }
            }
        }
    }
    return W;
}

// =====================================================================
// passive radar
// =====================================================================

void fftInPlace(Complex* data, int n, bool inverse) {
    if (n <= 1) return;
    // bit-reversal permutation
    for (int i = 1, j = 0; i < n; ++i) {
        int bit = n >> 1;
        for (; j & bit; bit >>= 1) j ^= bit;
        j ^= bit;
        if (i < j) std::swap(data[i], data[j]);
    }
    for (int len = 2; len <= n; len <<= 1) {
        const float angle = (inverse ? 2.0f : -2.0f) * kPi / static_cast<float>(len);
        const Complex wl(std::cos(angle), std::sin(angle));
        for (int i = 0; i < n; i += len) {
            Complex w(1.0f, 0.0f);
            for (int k = 0; k < len / 2; ++k) {
                const Complex u = data[i + k];
                const Complex v = data[i + k + len / 2] * w;
                data[i + k] = u + v;
                data[i + k + len / 2] = u - v;
                w *= wl;
            }
        }
    }
    if (inverse) {
        for (int i = 0; i < n; ++i) data[i] /= static_cast<float>(n);
    }
}

ComplexVec ecaCancel(const Complex* surveillance, const Complex* reference,
                     int n, int numTaps, float regularisation) {
    const int taps = std::max(1, std::min(numTaps, n));
    // Gram matrix G = X^H X and rhs = X^H s, where X[:,k] = ref delayed by k.
    std::vector<Complex> G(static_cast<size_t>(taps) * taps, Complex(0.0f, 0.0f));
    std::vector<Complex> rhs(taps, Complex(0.0f, 0.0f));

    for (int p = 0; p < taps; ++p) {
        for (int q = p; q < taps; ++q) {
            Complex acc(0.0f, 0.0f);
            const int start = std::max(p, q);
            for (int i = start; i < n; ++i) {
                acc += std::conj(reference[i - p]) * reference[i - q];
            }
            G[static_cast<size_t>(p) * taps + q] = acc;
            G[static_cast<size_t>(q) * taps + p] = std::conj(acc);
        }
        G[static_cast<size_t>(p) * taps + p] += Complex(regularisation, 0.0f);

        Complex acc(0.0f, 0.0f);
        for (int i = p; i < n; ++i) acc += std::conj(reference[i - p]) * surveillance[i];
        rhs[p] = acc;
    }

    // Solve G w = rhs by Gaussian elimination with partial pivoting.
    std::vector<Complex> M(G);
    std::vector<Complex> w(rhs);
    for (int col = 0; col < taps; ++col) {
        int pivot = col;
        float best = std::abs(M[static_cast<size_t>(col) * taps + col]);
        for (int r = col + 1; r < taps; ++r) {
            const float mag = std::abs(M[static_cast<size_t>(r) * taps + col]);
            if (mag > best) { best = mag; pivot = r; }
        }
        if (best < 1e-20f) continue;
        if (pivot != col) {
            for (int c = 0; c < taps; ++c) {
                std::swap(M[static_cast<size_t>(col) * taps + c],
                          M[static_cast<size_t>(pivot) * taps + c]);
            }
            std::swap(w[col], w[pivot]);
        }
        const Complex diag = M[static_cast<size_t>(col) * taps + col];
        for (int r = col + 1; r < taps; ++r) {
            const Complex factor = M[static_cast<size_t>(r) * taps + col] / diag;
            if (std::abs(factor) < 1e-30f) continue;
            for (int c = col; c < taps; ++c) {
                M[static_cast<size_t>(r) * taps + c] -=
                    factor * M[static_cast<size_t>(col) * taps + c];
            }
            w[r] -= factor * w[col];
        }
    }
    for (int r = taps - 1; r >= 0; --r) {
        Complex sum = w[r];
        for (int c = r + 1; c < taps; ++c) {
            sum -= M[static_cast<size_t>(r) * taps + c] * w[c];
        }
        const Complex diag = M[static_cast<size_t>(r) * taps + r];
        w[r] = (std::abs(diag) < 1e-20f) ? Complex(0.0f, 0.0f) : sum / diag;
    }

    ComplexVec out(static_cast<size_t>(n));
    for (int i = 0; i < n; ++i) {
        Complex estimate(0.0f, 0.0f);
        const int maxTap = std::min(taps - 1, i);
        for (int k = 0; k <= maxTap; ++k) estimate += w[k] * reference[i - k];
        out[i] = surveillance[i] - estimate;
    }
    return out;
}

float cancellationRatioDb(const Complex* before, const Complex* after, int n) {
    double pBefore = 0.0, pAfter = 0.0;
    for (int i = 0; i < n; ++i) {
        pBefore += std::norm(before[i]);
        pAfter += std::norm(after[i]);
    }
    if (pAfter <= 1e-30 || pBefore <= 1e-30) return 0.0f;
    return static_cast<float>(10.0 * std::log10(pBefore / pAfter));
}

RangeDopplerMap computeCaf(const Complex* surveillance, const Complex* reference,
                           int n, float sampleRate, int maxRangeBins, int numBatches) {
    RangeDopplerMap map;
    if (n < 8) return map;

    // round the batch count down to a power of two so the FFT stays radix-2
    int batches = std::max(2, std::min(numBatches, n / 4));
    int pow2 = 1;
    while (pow2 * 2 <= batches) pow2 *= 2;
    batches = pow2;

    const int batchLen = n / batches;
    if (batchLen < 2) return map;
    const int lags = std::max(1, std::min(maxRangeBins, batchLen - 1));

    map.rangeBins = lags;
    map.dopplerBins = batches;
    map.sampleRate = sampleRate;
    map.integrationTime = static_cast<float>(n) / sampleRate;
    map.magnitude.assign(static_cast<size_t>(batches) * lags, 0.0f);

    std::vector<Complex> column(batches);
    for (int lag = 0; lag < lags; ++lag) {
        for (int b = 0; b < batches; ++b) {
            const int s0 = b * batchLen;
            Complex acc(0.0f, 0.0f);
            for (int i = lag; i < batchLen; ++i) {
                acc += std::conj(reference[s0 + i - lag]) * surveillance[s0 + i];
            }
            column[b] = acc;
        }
        fftInPlace(column.data(), batches, false);
        // fftshift so zero Doppler sits in the middle, matching NumPy
        for (int b = 0; b < batches; ++b) {
            const int shifted = (b + batches / 2) % batches;
            map.magnitude[static_cast<size_t>(b) * lags + lag] = std::abs(column[shifted]);
        }
    }
    return map;
}

std::vector<uint8_t> cfar2d(const float* magnitude, int rows, int cols,
                            int guardR, int guardD, int trainR, int trainD,
                            float thresholdDb) {
    std::vector<uint8_t> mask(static_cast<size_t>(rows) * cols, 0);
    const float linear = std::pow(10.0f, thresholdDb / 10.0f);
    for (int j = 0; j < rows; ++j) {
        const int j0 = std::max(0, j - trainD), j1 = std::min(rows, j + trainD + 1);
        const int gj0 = std::max(0, j - guardD), gj1 = std::min(rows, j + guardD + 1);
        for (int i = 0; i < cols; ++i) {
            const int i0 = std::max(0, i - trainR), i1 = std::min(cols, i + trainR + 1);
            const int gi0 = std::max(0, i - guardR), gi1 = std::min(cols, i + guardR + 1);
            double total = 0.0;
            int count = 0;
            for (int jj = j0; jj < j1; ++jj) {
                for (int ii = i0; ii < i1; ++ii) {
                    const float m = magnitude[static_cast<size_t>(jj) * cols + ii];
                    total += static_cast<double>(m) * m;
                    ++count;
                }
            }
            for (int jj = gj0; jj < gj1; ++jj) {
                for (int ii = gi0; ii < gi1; ++ii) {
                    const float m = magnitude[static_cast<size_t>(jj) * cols + ii];
                    total -= static_cast<double>(m) * m;
                    --count;
                }
            }
            if (count <= 0) continue;
            const double noise = total / count;
            if (noise <= 1e-30) continue;
            const float cell = magnitude[static_cast<size_t>(j) * cols + i];
            if (static_cast<double>(cell) * cell > linear * noise) {
                mask[static_cast<size_t>(j) * cols + i] = 1;
            }
        }
    }
    return mask;
}

// =====================================================================
// EKF
// =====================================================================

float wrapPi(float angle) {
    float wrapped = std::fmod(angle + kPi, 2.0f * kPi);
    if (wrapped <= 0.0f) wrapped += 2.0f * kPi;
    return wrapped - kPi;
}

void eulerToMatrix(float roll, float pitch, float yaw, float* R) {
    const float cr = std::cos(roll), sr = std::sin(roll);
    const float cp = std::cos(pitch), sp = std::sin(pitch);
    const float cy = std::cos(yaw), sy = std::sin(yaw);
    R[0] = cy * cp;  R[1] = cy * sp * sr - sy * cr;  R[2] = cy * sp * cr + sy * sr;
    R[3] = sy * cp;  R[4] = sy * sp * sr + cy * cr;  R[5] = sy * sp * cr - cy * sr;
    R[6] = -sp;      R[7] = cp * sr;                 R[8] = cp * cr;
}

ExtendedKalmanFilter::ExtendedKalmanFilter() { reset(); }

void ExtendedKalmanFilter::reset() {
    std::memset(x_, 0, sizeof(x_));
    std::memset(P_, 0, sizeof(P_));
    for (int i = 0; i < 3; ++i) P_[i * kStateDim + i] = 4.0f;              // position
    for (int i = 3; i < 6; ++i) P_[i * kStateDim + i] = 1.0f;              // velocity
    for (int i = 6; i < 9; ++i) P_[i * kStateDim + i] = 0.25f;             // attitude
    for (int i = 9; i < 12; ++i) P_[i * kStateDim + i] = 1e-3f;            // gyro bias
    for (int i = 12; i < 15; ++i) P_[i * kStateDim + i] = 1e-2f;           // accel bias
}

void ExtendedKalmanFilter::symmetrise() {
    for (int i = 0; i < kStateDim; ++i) {
        for (int j = i + 1; j < kStateDim; ++j) {
            const float avg = 0.5f * (P_[i * kStateDim + j] + P_[j * kStateDim + i]);
            P_[i * kStateDim + j] = avg;
            P_[j * kStateDim + i] = avg;
        }
        if (P_[i * kStateDim + i] < 1e-9f) P_[i * kStateDim + i] = 1e-9f;
    }
}

void ExtendedKalmanFilter::predict(const float gyro[3], const float accel[3], float dt) {
    dt = std::min(std::max(dt, 1e-4f), maxDt);

    float w[3], aBody[3];
    for (int i = 0; i < 3; ++i) {
        w[i] = gyro[i] - x_[9 + i];
        aBody[i] = accel[i] - x_[12 + i];
    }
    const float roll = x_[6], pitch = x_[7], yaw = x_[8];
    float R[9];
    eulerToMatrix(roll, pitch, yaw, R);

    float aMap[3];
    for (int i = 0; i < 3; ++i) {
        aMap[i] = R[i * 3] * aBody[0] + R[i * 3 + 1] * aBody[1] + R[i * 3 + 2] * aBody[2];
    }
    aMap[2] -= kGravity;

    for (int i = 0; i < 3; ++i) {
        x_[i] += x_[3 + i] * dt + 0.5f * aMap[i] * dt * dt;
        x_[3 + i] += aMap[i] * dt;
    }

    float cp = std::cos(pitch);
    if (std::fabs(cp) < 1e-4f) cp = std::copysign(1e-4f, cp == 0.0f ? 1.0f : cp);
    const float tp = std::tan(pitch);
    const float sr = std::sin(roll), cr = std::cos(roll);
    const float T[9] = {1.0f, sr * tp, cr * tp,
                        0.0f, cr,      -sr,
                        0.0f, sr / cp, cr / cp};
    for (int i = 0; i < 3; ++i) {
        const float rate = T[i * 3] * w[0] + T[i * 3 + 1] * w[1] + T[i * 3 + 2] * w[2];
        x_[6 + i] = wrapPi(x_[6 + i] + rate * dt);
    }

    // F = I + dF
    float F[kStateDim * kStateDim];
    std::memset(F, 0, sizeof(F));
    for (int i = 0; i < kStateDim; ++i) F[i * kStateDim + i] = 1.0f;
    for (int i = 0; i < 3; ++i) F[i * kStateDim + (3 + i)] = dt;

    // d(R a)/d(euler), analytic - mirrors _d_rotation_d_euler in Python
    const float cy = std::cos(yaw), sy = std::sin(yaw);
    const float sp = std::sin(pitch);
    const float ax = aBody[0], ay = aBody[1], az = aBody[2];
    const float dRoll[3] = {
        (cy * sp * cr + sy * sr) * ay + (-cy * sp * sr + sy * cr) * az,
        (sy * sp * cr - cy * sr) * ay + (-sy * sp * sr - cy * cr) * az,
        (cp * cr) * ay + (-cp * sr) * az};
    const float dPitch[3] = {
        (-cy * sp) * ax + (cy * cp * sr) * ay + (cy * cp * cr) * az,
        (-sy * sp) * ax + (sy * cp * sr) * ay + (sy * cp * cr) * az,
        (-cp) * ax + (-sp * sr) * ay + (-sp * cr) * az};
    const float dYaw[3] = {
        (-sy * cp) * ax + (-sy * sp * sr - cy * cr) * ay + (-sy * sp * cr + cy * sr) * az,
        (cy * cp) * ax + (cy * sp * sr - sy * cr) * ay + (cy * sp * cr + sy * sr) * az,
        0.0f};
    for (int i = 0; i < 3; ++i) {
        F[(3 + i) * kStateDim + 6] = dRoll[i] * dt;
        F[(3 + i) * kStateDim + 7] = dPitch[i] * dt;
        F[(3 + i) * kStateDim + 8] = dYaw[i] * dt;
        for (int j = 0; j < 3; ++j) {
            F[(3 + i) * kStateDim + (12 + j)] = -R[i * 3 + j] * dt;
            F[(6 + i) * kStateDim + (9 + j)] = -T[i * 3 + j] * dt;
        }
    }

    // P = F P F^T + Q
    float FP[kStateDim * kStateDim];
    for (int i = 0; i < kStateDim; ++i) {
        for (int j = 0; j < kStateDim; ++j) {
            double sum = 0.0;
            for (int k = 0; k < kStateDim; ++k) {
                sum += static_cast<double>(F[i * kStateDim + k]) * P_[k * kStateDim + j];
            }
            FP[i * kStateDim + j] = static_cast<float>(sum);
        }
    }
    for (int i = 0; i < kStateDim; ++i) {
        for (int j = 0; j < kStateDim; ++j) {
            double sum = 0.0;
            for (int k = 0; k < kStateDim; ++k) {
                sum += static_cast<double>(FP[i * kStateDim + k]) * F[j * kStateDim + k];
            }
            P_[i * kStateDim + j] = static_cast<float>(sum);
        }
    }

    const float qa = sigmaAccel * sigmaAccel;
    const float qg = sigmaGyro * sigmaGyro;
    const float dt2 = dt * dt, dt3 = dt2 * dt, dt4 = dt3 * dt;
    for (int i = 0; i < 3; ++i) {
        P_[i * kStateDim + i] += 0.25f * qa * dt4;
        P_[(3 + i) * kStateDim + (3 + i)] += qa * dt2;
        P_[i * kStateDim + (3 + i)] += 0.5f * qa * dt3;
        P_[(3 + i) * kStateDim + i] += 0.5f * qa * dt3;
        P_[(6 + i) * kStateDim + (6 + i)] += qg * dt2;
        P_[(9 + i) * kStateDim + (9 + i)] += sigmaGyroBias * sigmaGyroBias * dt;
        P_[(12 + i) * kStateDim + (12 + i)] += sigmaAccelBias * sigmaAccelBias * dt;
    }
    symmetrise();
}

void ExtendedKalmanFilter::applyUpdate(const float* H, const float* innovation,
                                       const float* R, int m) {
    // S = H P H^T + R
    std::vector<float> PHt(static_cast<size_t>(kStateDim) * m, 0.0f);
    for (int i = 0; i < kStateDim; ++i) {
        for (int j = 0; j < m; ++j) {
            double sum = 0.0;
            for (int k = 0; k < kStateDim; ++k) {
                sum += static_cast<double>(P_[i * kStateDim + k]) * H[j * kStateDim + k];
            }
            PHt[static_cast<size_t>(i) * m + j] = static_cast<float>(sum);
        }
    }
    std::vector<float> S(static_cast<size_t>(m) * m, 0.0f);
    for (int i = 0; i < m; ++i) {
        for (int j = 0; j < m; ++j) {
            double sum = R[i * m + j];
            for (int k = 0; k < kStateDim; ++k) {
                sum += static_cast<double>(H[i * kStateDim + k]) * PHt[static_cast<size_t>(k) * m + j];
            }
            S[static_cast<size_t>(i) * m + j] = static_cast<float>(sum);
        }
    }
    // invert S (Gauss-Jordan; m is 1..3 in practice)
    std::vector<float> inv(static_cast<size_t>(m) * m, 0.0f);
    for (int i = 0; i < m; ++i) inv[static_cast<size_t>(i) * m + i] = 1.0f;
    for (int col = 0; col < m; ++col) {
        int pivot = col;
        float best = std::fabs(S[static_cast<size_t>(col) * m + col]);
        for (int r = col + 1; r < m; ++r) {
            const float mag = std::fabs(S[static_cast<size_t>(r) * m + col]);
            if (mag > best) { best = mag; pivot = r; }
        }
        if (best < 1e-20f) return;  // singular innovation covariance - skip
        if (pivot != col) {
            for (int c = 0; c < m; ++c) {
                std::swap(S[static_cast<size_t>(col) * m + c], S[static_cast<size_t>(pivot) * m + c]);
                std::swap(inv[static_cast<size_t>(col) * m + c], inv[static_cast<size_t>(pivot) * m + c]);
            }
        }
        const float diag = S[static_cast<size_t>(col) * m + col];
        for (int c = 0; c < m; ++c) {
            S[static_cast<size_t>(col) * m + c] /= diag;
            inv[static_cast<size_t>(col) * m + c] /= diag;
        }
        for (int r = 0; r < m; ++r) {
            if (r == col) continue;
            const float factor = S[static_cast<size_t>(r) * m + col];
            if (std::fabs(factor) < 1e-30f) continue;
            for (int c = 0; c < m; ++c) {
                S[static_cast<size_t>(r) * m + c] -= factor * S[static_cast<size_t>(col) * m + c];
                inv[static_cast<size_t>(r) * m + c] -= factor * inv[static_cast<size_t>(col) * m + c];
            }
        }
    }
    // K = P H^T S^-1
    std::vector<float> K(static_cast<size_t>(kStateDim) * m, 0.0f);
    for (int i = 0; i < kStateDim; ++i) {
        for (int j = 0; j < m; ++j) {
            double sum = 0.0;
            for (int k = 0; k < m; ++k) {
                sum += static_cast<double>(PHt[static_cast<size_t>(i) * m + k]) *
                       inv[static_cast<size_t>(k) * m + j];
            }
            K[static_cast<size_t>(i) * m + j] = static_cast<float>(sum);
        }
    }
    for (int i = 0; i < kStateDim; ++i) {
        double delta = 0.0;
        for (int j = 0; j < m; ++j) {
            delta += static_cast<double>(K[static_cast<size_t>(i) * m + j]) * innovation[j];
        }
        x_[i] += static_cast<float>(delta);
    }
    for (int i = 6; i < 9; ++i) x_[i] = wrapPi(x_[i]);

    // Joseph form: P = (I-KH) P (I-KH)^T + K R K^T
    std::vector<float> IKH(static_cast<size_t>(kStateDim) * kStateDim, 0.0f);
    for (int i = 0; i < kStateDim; ++i) {
        for (int j = 0; j < kStateDim; ++j) {
            double sum = (i == j) ? 1.0 : 0.0;
            for (int k = 0; k < m; ++k) {
                sum -= static_cast<double>(K[static_cast<size_t>(i) * m + k]) * H[k * kStateDim + j];
            }
            IKH[static_cast<size_t>(i) * kStateDim + j] = static_cast<float>(sum);
        }
    }
    std::vector<float> tmp(static_cast<size_t>(kStateDim) * kStateDim, 0.0f);
    for (int i = 0; i < kStateDim; ++i) {
        for (int j = 0; j < kStateDim; ++j) {
            double sum = 0.0;
            for (int k = 0; k < kStateDim; ++k) {
                sum += static_cast<double>(IKH[static_cast<size_t>(i) * kStateDim + k]) * P_[k * kStateDim + j];
            }
            tmp[static_cast<size_t>(i) * kStateDim + j] = static_cast<float>(sum);
        }
    }
    for (int i = 0; i < kStateDim; ++i) {
        for (int j = 0; j < kStateDim; ++j) {
            double sum = 0.0;
            for (int k = 0; k < kStateDim; ++k) {
                sum += static_cast<double>(tmp[static_cast<size_t>(i) * kStateDim + k]) *
                       IKH[static_cast<size_t>(j) * kStateDim + k];
            }
            for (int p = 0; p < m; ++p) {
                for (int q = 0; q < m; ++q) {
                    sum += static_cast<double>(K[static_cast<size_t>(i) * m + p]) * R[p * m + q] *
                           K[static_cast<size_t>(j) * m + q];
                }
            }
            P_[i * kStateDim + j] = static_cast<float>(sum);
        }
    }
    symmetrise();
}

void ExtendedKalmanFilter::updatePosition(const float position[3], float sigma) {
    float H[3 * kStateDim] = {0};
    float innovation[3], R[9] = {0};
    for (int i = 0; i < 3; ++i) {
        H[i * kStateDim + i] = 1.0f;
        innovation[i] = position[i] - x_[i];
        R[i * 3 + i] = sigma * sigma;
    }
    applyUpdate(H, innovation, R, 3);
}

void ExtendedKalmanFilter::updateUwbRange(const float anchor[3], float distance, float sigma) {
    const float dx = x_[0] - anchor[0];
    const float dy = x_[1] - anchor[1];
    const float dz = x_[2] - anchor[2];
    const float predicted = std::sqrt(dx * dx + dy * dy + dz * dz);
    if (predicted < 1e-6f) return;
    float H[kStateDim] = {0};
    H[0] = dx / predicted;
    H[1] = dy / predicted;
    H[2] = dz / predicted;
    const float innovation = distance - predicted;
    const float R = sigma * sigma;
    applyUpdate(H, &innovation, &R, 1);
}

void ExtendedKalmanFilter::updateYaw(float yaw, float sigma) {
    float H[kStateDim] = {0};
    H[8] = 1.0f;
    const float innovation = wrapPi(wrapPi(yaw) - x_[8]);
    const float R = sigma * sigma;
    applyUpdate(H, &innovation, &R, 1);
}

void ExtendedKalmanFilter::updateZeroVelocity(float sigma) {
    float H[3 * kStateDim] = {0};
    float innovation[3], R[9] = {0};
    for (int i = 0; i < 3; ++i) {
        H[i * kStateDim + (3 + i)] = 1.0f;
        innovation[i] = -x_[3 + i];
        R[i * 3 + i] = sigma * sigma;
    }
    applyUpdate(H, innovation, R, 3);
}

void ExtendedKalmanFilter::updateAltitude(float altitude, float sigma) {
    float H[kStateDim] = {0};
    H[2] = 1.0f;
    const float innovation = altitude - x_[2];
    const float R = sigma * sigma;
    applyUpdate(H, &innovation, &R, 1);
}

void ExtendedKalmanFilter::updateLidarPose(float x, float y, float yaw,
                                           float sigmaXy, float sigmaYaw) {
    // Deliberately 3 rows: a 2D scan match observes x, y and yaw only.
    // Feeding the filter's own z back would shrink the vertical covariance
    // without information and let the altitude drift.
    float H[3 * kStateDim] = {0};
    float innovation[3], R[9] = {0};
    H[0 * kStateDim + 0] = 1.0f;
    H[1 * kStateDim + 1] = 1.0f;
    H[2 * kStateDim + 8] = 1.0f;
    innovation[0] = x - x_[0];
    innovation[1] = y - x_[1];
    innovation[2] = wrapPi(wrapPi(yaw) - x_[8]);
    R[0] = sigmaXy * sigmaXy;
    R[4] = sigmaXy * sigmaXy;
    R[8] = sigmaYaw * sigmaYaw;
    applyUpdate(H, innovation, R, 3);
}

void ExtendedKalmanFilter::positionSigma(float out[3]) const {
    for (int i = 0; i < 3; ++i) out[i] = std::sqrt(std::max(P_[i * kStateDim + i], 0.0f));
}

void ExtendedKalmanFilter::quaternion(float out[4]) const {
    float R[9];
    eulerToMatrix(x_[6], x_[7], x_[8], R);
    const float trace = R[0] + R[4] + R[8];
    if (trace > 0.0f) {
        const float s = 0.5f / std::sqrt(trace + 1.0f);
        out[3] = 0.25f / s;
        out[0] = (R[7] - R[5]) * s;
        out[1] = (R[2] - R[6]) * s;
        out[2] = (R[3] - R[1]) * s;
    } else if (R[0] > R[4] && R[0] > R[8]) {
        const float s = 2.0f * std::sqrt(std::max(1e-12f, 1.0f + R[0] - R[4] - R[8]));
        out[3] = (R[7] - R[5]) / s;
        out[0] = 0.25f * s;
        out[1] = (R[1] + R[3]) / s;
        out[2] = (R[2] + R[6]) / s;
    } else if (R[4] > R[8]) {
        const float s = 2.0f * std::sqrt(std::max(1e-12f, 1.0f + R[4] - R[0] - R[8]));
        out[3] = (R[2] - R[6]) / s;
        out[0] = (R[1] + R[3]) / s;
        out[1] = 0.25f * s;
        out[2] = (R[5] + R[7]) / s;
    } else {
        const float s = 2.0f * std::sqrt(std::max(1e-12f, 1.0f + R[8] - R[0] - R[4]));
        out[3] = (R[3] - R[1]) / s;
        out[0] = (R[2] + R[6]) / s;
        out[1] = (R[5] + R[7]) / s;
        out[2] = 0.25f * s;
    }
}

// =====================================================================
// voxel RLE codec
// =====================================================================

std::vector<uint8_t> encodeChunk(const uint16_t* intensity, const uint8_t* label, int size) {
    const int total = size * size * size;
    std::vector<uint8_t> out;
    out.reserve(64);
    out.push_back('A'); out.push_back('V'); out.push_back('X'); out.push_back('1');
    out.push_back(static_cast<uint8_t>(size));
    const size_t runCountOffset = out.size();
    out.insert(out.end(), 4, 0);  // placeholder for the run count

    uint32_t runs = 0;
    uint16_t curI = intensity[0];
    uint8_t curL = label[0];
    uint16_t count = 0;
    auto flush = [&]() {
        out.push_back(static_cast<uint8_t>(count & 0xFF));
        out.push_back(static_cast<uint8_t>(count >> 8));
        out.push_back(static_cast<uint8_t>(curI & 0xFF));
        out.push_back(static_cast<uint8_t>(curI >> 8));
        out.push_back(curL);
        ++runs;
    };
    for (int i = 0; i < total; ++i) {
        if (intensity[i] == curI && label[i] == curL && count < 65535) {
            ++count;
        } else {
            flush();
            curI = intensity[i];
            curL = label[i];
            count = 1;
        }
    }
    flush();
    out[runCountOffset + 0] = static_cast<uint8_t>(runs & 0xFF);
    out[runCountOffset + 1] = static_cast<uint8_t>((runs >> 8) & 0xFF);
    out[runCountOffset + 2] = static_cast<uint8_t>((runs >> 16) & 0xFF);
    out[runCountOffset + 3] = static_cast<uint8_t>((runs >> 24) & 0xFF);
    return out;
}

bool decodeChunk(const uint8_t* blob, size_t length, uint16_t* intensity,
                 uint8_t* label, int size) {
    if (length < 9) return false;
    if (blob[0] != 'A' || blob[1] != 'V' || blob[2] != 'X' || blob[3] != '1') return false;
    if (static_cast<int>(blob[4]) != size) return false;
    const uint32_t runs = static_cast<uint32_t>(blob[5]) |
                          (static_cast<uint32_t>(blob[6]) << 8) |
                          (static_cast<uint32_t>(blob[7]) << 16) |
                          (static_cast<uint32_t>(blob[8]) << 24);
    const int total = size * size * size;
    size_t offset = 9;
    int cursor = 0;
    for (uint32_t r = 0; r < runs; ++r) {
        if (offset + 5 > length) return false;
        const uint16_t count = static_cast<uint16_t>(blob[offset]) |
                               (static_cast<uint16_t>(blob[offset + 1]) << 8);
        const uint16_t value = static_cast<uint16_t>(blob[offset + 2]) |
                               (static_cast<uint16_t>(blob[offset + 3]) << 8);
        const uint8_t lab = blob[offset + 4];
        offset += 5;
        const int end = std::min(cursor + static_cast<int>(count), total);
        for (int i = cursor; i < end; ++i) {
            intensity[i] = value;
            label[i] = lab;
        }
        cursor = end;
    }
    return true;
}

}  // namespace aura
