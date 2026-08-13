// aura_jni.cpp - JNI surface of the AURA 6.0 native core.
//
// Marshalling rules used throughout:
//   * Bulk numeric data crosses as direct ByteBuffers or primitive arrays
//     released with JNI_ABORT when we did not modify them, so the JVM never
//     copies back.
//   * Long-lived objects (EKF, RTI context) are held natively and referenced
//     from Kotlin by an opaque handle, avoiding per-call reallocation.
//   * Nothing throws across the JNI boundary; failures return null / false.

#include <jni.h>

#include <cmath>
#include <cstring>
#include <memory>
#include <mutex>
#include <unordered_map>
#include <vector>

#include "aura_core.h"

namespace {

std::mutex g_mutex;
jlong g_nextHandle = 1;
std::unordered_map<jlong, std::unique_ptr<aura::ExtendedKalmanFilter>> g_filters;

struct RtiContext {
    std::vector<float> weights;
    int links = 0;
    int voxels = 0;
    float lipschitz = 0.0f;
};
std::unordered_map<jlong, std::unique_ptr<RtiContext>> g_rti;

aura::ExtendedKalmanFilter* filterFor(jlong handle) {
    std::lock_guard<std::mutex> lock(g_mutex);
    auto it = g_filters.find(handle);
    return it == g_filters.end() ? nullptr : it->second.get();
}

RtiContext* rtiFor(jlong handle) {
    std::lock_guard<std::mutex> lock(g_mutex);
    auto it = g_rti.find(handle);
    return it == g_rti.end() ? nullptr : it->second.get();
}

}  // namespace

extern "C" {

// =====================================================================
// EKF
// =====================================================================

JNIEXPORT jlong JNICALL
Java_com_aura_agent_fusion_NativeEkf_nativeCreate(JNIEnv*, jobject) {
    std::lock_guard<std::mutex> lock(g_mutex);
    const jlong handle = g_nextHandle++;
    g_filters[handle] = std::make_unique<aura::ExtendedKalmanFilter>();
    return handle;
}

JNIEXPORT void JNICALL
Java_com_aura_agent_fusion_NativeEkf_nativeDestroy(JNIEnv*, jobject, jlong handle) {
    std::lock_guard<std::mutex> lock(g_mutex);
    g_filters.erase(handle);
}

JNIEXPORT void JNICALL
Java_com_aura_agent_fusion_NativeEkf_nativeReset(JNIEnv*, jobject, jlong handle) {
    if (auto* ekf = filterFor(handle)) ekf->reset();
}

JNIEXPORT void JNICALL
Java_com_aura_agent_fusion_NativeEkf_nativePredict(JNIEnv* env, jobject, jlong handle,
                                                   jfloatArray gyro, jfloatArray accel,
                                                   jfloat dt) {
    auto* ekf = filterFor(handle);
    if (!ekf || env->GetArrayLength(gyro) < 3 || env->GetArrayLength(accel) < 3) return;
    jfloat* g = env->GetFloatArrayElements(gyro, nullptr);
    jfloat* a = env->GetFloatArrayElements(accel, nullptr);
    ekf->predict(g, a, dt);
    env->ReleaseFloatArrayElements(gyro, g, JNI_ABORT);
    env->ReleaseFloatArrayElements(accel, a, JNI_ABORT);
}

JNIEXPORT void JNICALL
Java_com_aura_agent_fusion_NativeEkf_nativeUpdatePosition(JNIEnv* env, jobject, jlong handle,
                                                          jfloatArray position, jfloat sigma) {
    auto* ekf = filterFor(handle);
    if (!ekf || env->GetArrayLength(position) < 3) return;
    jfloat* p = env->GetFloatArrayElements(position, nullptr);
    ekf->updatePosition(p, sigma);
    env->ReleaseFloatArrayElements(position, p, JNI_ABORT);
}

JNIEXPORT void JNICALL
Java_com_aura_agent_fusion_NativeEkf_nativeUpdateUwbRange(JNIEnv* env, jobject, jlong handle,
                                                          jfloatArray anchor, jfloat distance,
                                                          jfloat sigma) {
    auto* ekf = filterFor(handle);
    if (!ekf || env->GetArrayLength(anchor) < 3) return;
    jfloat* a = env->GetFloatArrayElements(anchor, nullptr);
    ekf->updateUwbRange(a, distance, sigma);
    env->ReleaseFloatArrayElements(anchor, a, JNI_ABORT);
}

JNIEXPORT void JNICALL
Java_com_aura_agent_fusion_NativeEkf_nativeUpdateYaw(JNIEnv*, jobject, jlong handle,
                                                     jfloat yaw, jfloat sigma) {
    if (auto* ekf = filterFor(handle)) ekf->updateYaw(yaw, sigma);
}

JNIEXPORT void JNICALL
Java_com_aura_agent_fusion_NativeEkf_nativeUpdateZeroVelocity(JNIEnv*, jobject, jlong handle,
                                                              jfloat sigma) {
    if (auto* ekf = filterFor(handle)) ekf->updateZeroVelocity(sigma);
}

JNIEXPORT void JNICALL
Java_com_aura_agent_fusion_NativeEkf_nativeUpdateAltitude(JNIEnv*, jobject, jlong handle,
                                                          jfloat altitude, jfloat sigma) {
    if (auto* ekf = filterFor(handle)) ekf->updateAltitude(altitude, sigma);
}

JNIEXPORT void JNICALL
Java_com_aura_agent_fusion_NativeEkf_nativeUpdateLidarPose(JNIEnv*, jobject, jlong handle,
                                                           jfloat x, jfloat y, jfloat yaw,
                                                           jfloat sigmaXy, jfloat sigmaYaw) {
    if (auto* ekf = filterFor(handle)) ekf->updateLidarPose(x, y, yaw, sigmaXy, sigmaYaw);
}

JNIEXPORT jfloatArray JNICALL
Java_com_aura_agent_fusion_NativeEkf_nativeGetState(JNIEnv* env, jobject, jlong handle) {
    auto* ekf = filterFor(handle);
    if (!ekf) return nullptr;
    jfloatArray out = env->NewFloatArray(aura::kStateDim);
    if (!out) return nullptr;
    env->SetFloatArrayRegion(out, 0, aura::kStateDim, ekf->state());
    return out;
}

JNIEXPORT jfloatArray JNICALL
Java_com_aura_agent_fusion_NativeEkf_nativeGetCovarianceDiagonal(JNIEnv* env, jobject,
                                                                 jlong handle) {
    auto* ekf = filterFor(handle);
    if (!ekf) return nullptr;
    float diagonal[aura::kStateDim];
    const float* P = ekf->covariance();
    for (int i = 0; i < aura::kStateDim; ++i) diagonal[i] = P[i * aura::kStateDim + i];
    jfloatArray out = env->NewFloatArray(aura::kStateDim);
    if (!out) return nullptr;
    env->SetFloatArrayRegion(out, 0, aura::kStateDim, diagonal);
    return out;
}

JNIEXPORT jfloatArray JNICALL
Java_com_aura_agent_fusion_NativeEkf_nativeGetQuaternion(JNIEnv* env, jobject, jlong handle) {
    auto* ekf = filterFor(handle);
    if (!ekf) return nullptr;
    float q[4];
    ekf->quaternion(q);
    jfloatArray out = env->NewFloatArray(4);
    if (!out) return nullptr;
    env->SetFloatArrayRegion(out, 0, 4, q);
    return out;
}

// =====================================================================
// RTI
// =====================================================================

JNIEXPORT jlong JNICALL
Java_com_aura_agent_rti_NativeRti_nativeCreate(JNIEnv* env, jobject, jfloatArray nodes,
                                               jfloat minX, jfloat minY, jfloat resolution,
                                               jint nx, jint ny, jfloat ellipseWidth) {
    const jsize length = env->GetArrayLength(nodes);
    if (length < 4 || nx <= 0 || ny <= 0) return 0;
    const int nodeCount = length / 2;

    jfloat* raw = env->GetFloatArrayElements(nodes, nullptr);
    auto context = std::make_unique<RtiContext>();
    context->weights = aura::buildWeightMatrix(raw, nodeCount, minX, minY, resolution,
                                               nx, ny, ellipseWidth);
    env->ReleaseFloatArrayElements(nodes, raw, JNI_ABORT);

    context->links = nodeCount * (nodeCount - 1) / 2;
    context->voxels = nx * ny;
    context->lipschitz = aura::powerIterationLipschitz(context->weights.data(),
                                                       context->links, context->voxels);
    std::lock_guard<std::mutex> lock(g_mutex);
    const jlong handle = g_nextHandle++;
    g_rti[handle] = std::move(context);
    return handle;
}

JNIEXPORT void JNICALL
Java_com_aura_agent_rti_NativeRti_nativeDestroy(JNIEnv*, jobject, jlong handle) {
    std::lock_guard<std::mutex> lock(g_mutex);
    g_rti.erase(handle);
}

JNIEXPORT jfloatArray JNICALL
Java_com_aura_agent_rti_NativeRti_nativeReconstruct(JNIEnv* env, jobject, jlong handle,
                                                    jfloatArray measurements, jfloat alpha,
                                                    jint maxIter, jboolean useL1) {
    auto* context = rtiFor(handle);
    if (!context) return nullptr;
    if (env->GetArrayLength(measurements) < context->links) return nullptr;

    jfloat* y = env->GetFloatArrayElements(measurements, nullptr);
    std::vector<float> image;
    if (useL1) {
        auto result = aura::reconstructL1Fista(context->weights.data(), y, context->links,
                                               context->voxels, alpha, maxIter, 1e-6f, true,
                                               context->lipschitz);
        image = std::move(result.image);
    } else {
        image = aura::reconstructTikhonov(context->weights.data(), y, context->links,
                                          context->voxels, alpha);
        for (float& value : image) value = std::max(value, 0.0f);
    }
    env->ReleaseFloatArrayElements(measurements, y, JNI_ABORT);

    jfloatArray out = env->NewFloatArray(context->voxels);
    if (!out) return nullptr;
    env->SetFloatArrayRegion(out, 0, context->voxels, image.data());
    return out;
}

JNIEXPORT jfloat JNICALL
Java_com_aura_agent_rti_NativeRti_nativeGetLipschitz(JNIEnv*, jobject, jlong handle) {
    auto* context = rtiFor(handle);
    return context ? context->lipschitz : 0.0f;
}

JNIEXPORT jint JNICALL
Java_com_aura_agent_rti_NativeRti_nativeGetLinkCount(JNIEnv*, jobject, jlong handle) {
    auto* context = rtiFor(handle);
    return context ? context->links : 0;
}

// =====================================================================
// passive radar
// =====================================================================

// Interleaved float IQ in, magnitude map out. Direct ByteBuffers keep the
// 100k+ sample transfers zero-copy.
JNIEXPORT jfloatArray JNICALL
Java_com_aura_agent_radar_NativePassiveRadar_nativeProcess(
    JNIEnv* env, jobject, jobject survBuffer, jobject refBuffer, jint sampleCount,
    jfloat sampleRate, jint maxRangeBins, jint numBatches, jint ecaTaps) {
    auto* survRaw = static_cast<float*>(env->GetDirectBufferAddress(survBuffer));
    auto* refRaw = static_cast<float*>(env->GetDirectBufferAddress(refBuffer));
    if (!survRaw || !refRaw || sampleCount < 8) return nullptr;

    std::vector<aura::Complex> surv(sampleCount), ref(sampleCount);
    for (int i = 0; i < sampleCount; ++i) {
        surv[i] = aura::Complex(survRaw[2 * i], survRaw[2 * i + 1]);
        ref[i] = aura::Complex(refRaw[2 * i], refRaw[2 * i + 1]);
    }

    auto clean = ecaTaps > 0
                     ? aura::ecaCancel(surv.data(), ref.data(), sampleCount, ecaTaps)
                     : surv;
    auto map = aura::computeCaf(clean.data(), ref.data(), sampleCount, sampleRate,
                                maxRangeBins, numBatches);
    if (map.magnitude.empty()) return nullptr;

    // layout: [rangeBins, dopplerBins, integrationTime, ...magnitude]
    const int total = static_cast<int>(map.magnitude.size());
    jfloatArray out = env->NewFloatArray(total + 3);
    if (!out) return nullptr;
    const float header[3] = {static_cast<float>(map.rangeBins),
                             static_cast<float>(map.dopplerBins),
                             map.integrationTime};
    env->SetFloatArrayRegion(out, 0, 3, header);
    env->SetFloatArrayRegion(out, 3, total, map.magnitude.data());
    return out;
}

JNIEXPORT jbyteArray JNICALL
Java_com_aura_agent_radar_NativePassiveRadar_nativeCfar(JNIEnv* env, jobject,
                                                        jfloatArray magnitude, jint rows,
                                                        jint cols, jfloat thresholdDb) {
    if (env->GetArrayLength(magnitude) < rows * cols) return nullptr;
    jfloat* m = env->GetFloatArrayElements(magnitude, nullptr);
    auto mask = aura::cfar2d(m, rows, cols, 2, 2, 6, 6, thresholdDb);
    env->ReleaseFloatArrayElements(magnitude, m, JNI_ABORT);

    jbyteArray out = env->NewByteArray(static_cast<jsize>(mask.size()));
    if (!out) return nullptr;
    env->SetByteArrayRegion(out, 0, static_cast<jsize>(mask.size()),
                            reinterpret_cast<const jbyte*>(mask.data()));
    return out;
}

// =====================================================================
// voxel codec
// =====================================================================

JNIEXPORT jbyteArray JNICALL
Java_com_aura_agent_storage_NativeVoxelCodec_nativeEncode(JNIEnv* env, jobject,
                                                          jshortArray intensity,
                                                          jbyteArray label, jint size) {
    const int total = size * size * size;
    if (env->GetArrayLength(intensity) < total || env->GetArrayLength(label) < total) {
        return nullptr;
    }
    jshort* i16 = env->GetShortArrayElements(intensity, nullptr);
    jbyte* l8 = env->GetByteArrayElements(label, nullptr);
    auto blob = aura::encodeChunk(reinterpret_cast<const uint16_t*>(i16),
                                  reinterpret_cast<const uint8_t*>(l8), size);
    env->ReleaseShortArrayElements(intensity, i16, JNI_ABORT);
    env->ReleaseByteArrayElements(label, l8, JNI_ABORT);

    jbyteArray out = env->NewByteArray(static_cast<jsize>(blob.size()));
    if (!out) return nullptr;
    env->SetByteArrayRegion(out, 0, static_cast<jsize>(blob.size()),
                            reinterpret_cast<const jbyte*>(blob.data()));
    return out;
}

JNIEXPORT jboolean JNICALL
Java_com_aura_agent_storage_NativeVoxelCodec_nativeDecode(JNIEnv* env, jobject,
                                                          jbyteArray blob,
                                                          jshortArray intensityOut,
                                                          jbyteArray labelOut, jint size) {
    const int total = size * size * size;
    if (env->GetArrayLength(intensityOut) < total || env->GetArrayLength(labelOut) < total) {
        return JNI_FALSE;
    }
    jbyte* raw = env->GetByteArrayElements(blob, nullptr);
    const jsize length = env->GetArrayLength(blob);

    std::vector<uint16_t> intensity(total, 0);
    std::vector<uint8_t> label(total, 0);
    const bool ok = aura::decodeChunk(reinterpret_cast<const uint8_t*>(raw),
                                      static_cast<size_t>(length), intensity.data(),
                                      label.data(), size);
    env->ReleaseByteArrayElements(blob, raw, JNI_ABORT);
    if (!ok) return JNI_FALSE;

    env->SetShortArrayRegion(intensityOut, 0, total,
                             reinterpret_cast<const jshort*>(intensity.data()));
    env->SetByteArrayRegion(labelOut, 0, total,
                            reinterpret_cast<const jbyte*>(label.data()));
    return JNI_TRUE;
}

// =====================================================================
// library metadata
// =====================================================================

JNIEXPORT jstring JNICALL
Java_com_aura_agent_NativeEngine_nativeVersion(JNIEnv* env, jobject) {
#if defined(__ARM_NEON) || defined(__ARM_NEON__)
    return env->NewStringUTF("aura-core 6.0.0 (NEON)");
#else
    return env->NewStringUTF("aura-core 6.0.0 (scalar)");
#endif
}

JNIEXPORT jboolean JNICALL
Java_com_aura_agent_NativeEngine_nativeHasNeon(JNIEnv*, jobject) {
#if defined(__ARM_NEON) || defined(__ARM_NEON__)
    return JNI_TRUE;
#else
    return JNI_FALSE;
#endif
}

}  // extern "C"
