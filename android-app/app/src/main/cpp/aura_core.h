// aura_core.h - AURA 6.0 native signal-processing core.
//
// This header is the C++ counterpart of the verified Python reference in
// `edge-agent/aura/`. Every function here is a deliberate line-by-line port:
//
//   reconstructL1Fista   <-> aura.rti.reconstruct_l1_fista
//   powerIterationLipschitz <-> aura.rti.power_iteration_lipschitz
//   ecaCancel            <-> aura.passive_radar.eca_cancel
//   computeCaf           <-> aura.passive_radar.compute_caf_fft
//   ExtendedKalmanFilter <-> aura.ekf.ExtendedKalmanFilter
//
// `tests/native/test_aura_core.cpp` checks the C++ output against numbers
// produced by the Python implementation, so the two cannot silently diverge.
//
// Design constraints:
//   * No Eigen / FFTW / BLAS dependency. The CT45P build should not need a
//     third-party toolchain, and the matrices involved (15x15 EKF, a few
//     hundred RTI voxels) are small enough that a tuned dense kernel wins
//     over a general-purpose library once JNI marshalling is counted.
//   * ARM NEON is used when available (`__ARM_NEON`), with a scalar fallback
//     so the same file compiles for x86 CI and for arm64-v8a devices.
//   * Nothing allocates inside the hot loops; buffers are sized up front.

#ifndef AURA_CORE_H
#define AURA_CORE_H

#include <complex>
#include <cstddef>
#include <cstdint>
#include <vector>

namespace aura {

using Complex = std::complex<float>;
using ComplexVec = std::vector<Complex>;
using FloatVec = std::vector<float>;

// ---------------------------------------------------------------------
// linear algebra helpers (row-major dense)
// ---------------------------------------------------------------------

// y = A * x   (A is rows x cols, row-major)
void matVec(const float* A, const float* x, float* y, int rows, int cols);

// y = A^T * x
void matTVec(const float* A, const float* x, float* y, int rows, int cols);

// NEON-accelerated dot product with a scalar fallback.
float dot(const float* a, const float* b, int n);

// Euclidean norm.
float norm2(const float* a, int n);

// ---------------------------------------------------------------------
// Radio Tomographic Imaging
// ---------------------------------------------------------------------

struct FistaResult {
    FloatVec image;
    int iterations = 0;
    bool converged = false;
    float lipschitz = 0.0f;
    float objective = 0.0f;
};

// Largest eigenvalue of W^T W via power iteration.
//
// Using ||W||_F^2 instead (as many reference implementations do) over-
// estimates the Lipschitz constant by 4x or more on typical RTI geometries,
// which shrinks the FISTA step size and stalls convergence.
float powerIterationLipschitz(const float* W, int rows, int cols, int iterations = 60);

// L1 proximal operator: sign(x) * max(|x| - t, 0).
void softThreshold(float* x, int n, float threshold, bool nonNegative);

// Compressed-sensing reconstruction: min 0.5*||Wx - y||^2 + alpha*||x||_1
FistaResult reconstructL1Fista(const float* W, const float* y, int rows, int cols,
                               float alpha = 0.1f, int maxIter = 200,
                               float tolerance = 1e-6f, bool nonNegative = true,
                               float lipschitz = -1.0f);

// Tikhonov (L2) baseline: (W^T W + alpha I) x = W^T y, solved by Cholesky.
FloatVec reconstructTikhonov(const float* W, const float* y, int rows, int cols,
                             float alpha = 0.1f);

// Build the ellipse-model RTI weight matrix. `nodes` is 2*nodeCount floats.
// Returns a rows x cols row-major matrix, rows = nodeCount*(nodeCount-1)/2.
std::vector<float> buildWeightMatrix(const float* nodes, int nodeCount,
                                     float minX, float minY, float resolution,
                                     int nx, int ny, float ellipseWidth = 0.35f);

// ---------------------------------------------------------------------
// Passive radar
// ---------------------------------------------------------------------

struct RangeDopplerMap {
    std::vector<float> magnitude;   // dopplerBins x rangeBins, row-major
    int rangeBins = 0;
    int dopplerBins = 0;
    float sampleRate = 0.0f;
    float integrationTime = 0.0f;
};

// Extensive Cancellation Algorithm: project the direct path and zero-Doppler
// clutter out of the surveillance channel.
ComplexVec ecaCancel(const Complex* surveillance, const Complex* reference,
                     int n, int numTaps = 32, float regularisation = 1e-6f);

// Cross-ambiguity function via the batches algorithm.
RangeDopplerMap computeCaf(const Complex* surveillance, const Complex* reference,
                           int n, float sampleRate, int maxRangeBins = 64,
                           int numBatches = 64);

// In-place radix-2 FFT. `n` must be a power of two.
void fftInPlace(Complex* data, int n, bool inverse = false);

// Cell-averaging CFAR over a range-Doppler map. Returns a byte mask.
std::vector<uint8_t> cfar2d(const float* magnitude, int rows, int cols,
                            int guardR = 2, int guardD = 2,
                            int trainR = 6, int trainD = 6,
                            float thresholdDb = 12.0f);

// Ratio of input to output power, in dB.
float cancellationRatioDb(const Complex* before, const Complex* after, int n);

// ---------------------------------------------------------------------
// 15-state EKF (port of aura.ekf)
// ---------------------------------------------------------------------

constexpr int kStateDim = 15;

class ExtendedKalmanFilter {
public:
    ExtendedKalmanFilter();

    void reset();
    void predict(const float gyro[3], const float accel[3], float dt);
    void updatePosition(const float position[3], float sigma);
    void updateUwbRange(const float anchor[3], float distance, float sigma);
    void updateYaw(float yaw, float sigma);
    void updateZeroVelocity(float sigma = 0.02f);
    void updateAltitude(float altitude, float sigma);
    // 2D scan match: observes (x, y, yaw) only - never the altitude.
    void updateLidarPose(float x, float y, float yaw, float sigmaXy, float sigmaYaw);

    const float* state() const { return x_; }
    const float* covariance() const { return P_; }
    void positionSigma(float out[3]) const;
    void quaternion(float out[4]) const;

    float maxDt = 0.5f;
    // Chi-square gate on the normalised innovation; 16 == 4 sigma at 1 DoF.
    float gateThreshold = 16.0f;
    // After this many consecutive rejections the filter distrusts itself
    // rather than the sensor and accepts one update to recover.
    int maxConsecutiveRejects = 5;
    int rejected() const { return rejected_; }
    float sigmaAccel = 0.35f;
    float sigmaGyro = 0.02f;
    float sigmaGyroBias = 1.5e-4f;
    float sigmaAccelBias = 8.0e-4f;

private:
    void applyUpdate(const float* H, const float* innovation, const float* R, int m,
                     int sourceId = 0);
    void symmetrise();
    int rejected_ = 0;
    // Per-source streaks: two anchors can be locked out permanently while the
    // other two keep resetting a shared counter. kMaxSources covers the
    // update kinds plus a slot per anchor hash.
    // 64 slots: collisions merely merge two anchors' streaks, which weakens
    // the escape hatch slightly rather than breaking correctness.
    static constexpr int kMaxSources = 64;
    int consecutiveRejects_[kMaxSources] = {0};

    float x_[kStateDim];
    float P_[kStateDim * kStateDim];
};

// Wrap an angle to (-pi, pi].
float wrapPi(float angle);

// ZYX Euler -> 3x3 row-major rotation matrix (body to map).
void eulerToMatrix(float roll, float pitch, float yaw, float* R);

// ---------------------------------------------------------------------
// Voxel RLE codec (binary-compatible with aura.voxel / SpatialChunkEntity)
// ---------------------------------------------------------------------

// Encode a size^3 chunk. Layout: 'AVX1' | uint8 size | uint32 runs |
// runs * (uint16 count, uint16 intensity, uint8 label)
std::vector<uint8_t> encodeChunk(const uint16_t* intensity, const uint8_t* label, int size);

// Decode; returns false when the blob is malformed.
bool decodeChunk(const uint8_t* blob, size_t length, uint16_t* intensity,
                 uint8_t* label, int size);

}  // namespace aura

#endif  // AURA_CORE_H
