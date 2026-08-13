// jni_stub.h - a minimal jni.h for host-side compile checks.
//
// The Android NDK is not available in every environment (and not in the one
// this project was authored in), but the JNI layer is exactly where a silent
// mistake becomes an UnsatisfiedLinkError on the device rather than a build
// error. This stub declares just enough of the JNI surface to type-check
// aura_jni.cpp and llama_bridge.cpp with a plain g++ and to emit the mangled
// symbols, which tools/check-jni-symbols.py then cross-checks against the
// Kotlin `external fun` declarations.
//
// It is NOT a functional JNI implementation - every method returns a dummy
// value. Never link it into anything that runs.

#ifndef JNI_STUB_H
#define JNI_STUB_H
#include <cstdint>
#include <cstddef>
typedef int32_t jint; typedef int64_t jlong; typedef float jfloat; typedef int8_t jbyte;
typedef int16_t jshort; typedef uint8_t jboolean; typedef int32_t jsize;
#define JNI_TRUE 1
#define JNI_FALSE 0
#define JNI_ABORT 2
#define JNIEXPORT
#define JNICALL
struct _jobject; typedef _jobject* jobject; typedef jobject jstring;
typedef jobject jfloatArray; typedef jobject jbyteArray; typedef jobject jshortArray;
struct JNIEnv {
  jsize GetArrayLength(jobject){return 0;}
  jfloat* GetFloatArrayElements(jfloatArray, void*){return nullptr;}
  void ReleaseFloatArrayElements(jfloatArray, jfloat*, jint){}
  jshort* GetShortArrayElements(jshortArray, void*){return nullptr;}
  void ReleaseShortArrayElements(jshortArray, jshort*, jint){}
  jbyte* GetByteArrayElements(jbyteArray, void*){return nullptr;}
  void ReleaseByteArrayElements(jbyteArray, jbyte*, jint){}
  jfloatArray NewFloatArray(jsize){return nullptr;}
  jbyteArray NewByteArray(jsize){return nullptr;}
  void SetFloatArrayRegion(jfloatArray, jsize, jsize, const jfloat*){}
  void SetByteArrayRegion(jbyteArray, jsize, jsize, const jbyte*){}
  void SetShortArrayRegion(jshortArray, jsize, jsize, const jshort*){}
  void* GetDirectBufferAddress(jobject){return nullptr;}
  jstring NewStringUTF(const char*){return nullptr;}
  const char* GetStringUTFChars(jstring, jboolean*){return nullptr;}
  void ReleaseStringUTFChars(jstring, const char*){}
  jsize GetStringUTFLength(jstring){return 0;}
};
#endif
