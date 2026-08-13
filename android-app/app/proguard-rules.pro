# R8 / ProGuard rules for the AURA agent.
#
# Referenced by build.gradle.kts. Without this file the release build fails
# outright; without the rules in it, the release build *succeeds* and then
# crashes on device, which is worse.
#
# The theme throughout: R8 renames and strips anything it cannot see being
# used. Every binding below is resolved by *name* at runtime — from C++, from
# reflection, or from the Android framework — so R8 cannot see the use and
# will happily break it.

# ---------------------------------------------------------------- JNI
# A JNI symbol is Java_<package>_<Class>_<method>. Renaming either side
# desynchronises the pair and the method throws UnsatisfiedLinkError the first
# time it is called — in release builds only, so it survives every debug test.
# tools/check-jni-symbols.py verifies these names at build time; these rules
# stop R8 invalidating that guarantee afterwards.
-keepclasseswithmembernames,includedescriptorclasses class * {
    native <methods>;
}

# The classes that declare them, including their nested/companion shapes.
-keep class com.aura.agent.AuraApplication { *; }
-keep class com.aura.agent.NativeEngine { *; }
-keep class com.aura.agent.fusion.NativeEkf { *; }
-keep class com.aura.agent.llm.LLMService { *; }
-keep class com.aura.agent.radar.NativePassiveRadar { *; }
-keep class com.aura.agent.rti.NativeRti { *; }
-keep class com.aura.agent.storage.NativeVoxelCodec { *; }

# Types the native side constructs or reads back. If C++ does a FindClass or
# GetFieldID on these, a renamed field is a null return at runtime.
-keep class com.aura.agent.fusion.EkfSnapshot { *; }
-keep class com.aura.agent.rti.RtiTarget { *; }
-keep class com.aura.agent.rti.RtiNode { *; }
-keep class com.aura.agent.radar.RangeDopplerMap { *; }

# ---------------------------------------------------- reflective lookups
# UwbManager reaches androidx.core.uwb via Class.forName so the class is never
# resolved on API 30 (see docs/source_claims.md). R8 cannot see that use, and
# -dontwarn is required because the dependency is deliberately not declared.
-dontwarn androidx.core.uwb.**
-keep class androidx.core.uwb.** { *; }

# ------------------------------------------------ framework entry points
# Instantiated by name from the manifest.
-keep class com.aura.agent.ui.MainActivity { *; }
-keep class com.aura.agent.fusion.SensorFusionService { *; }
-keep class com.aura.agent.network.GatekeeperVpnService { *; }
-keep class * extends android.app.Application
-keep class * extends android.app.Service
-keep class * extends androidx.fragment.app.Fragment

# Custom views are inflated from XML by name, with the two-argument
# (Context, AttributeSet) constructor.
-keep class com.aura.agent.ui.AttitudeView { *; }
-keep class com.aura.agent.ui.PointCloudView { *; }
-keep class com.aura.agent.ui.RssiBarView { *; }
-keepclasseswithmembers class * {
    public <init>(android.content.Context, android.util.AttributeSet);
}

# ------------------------------------------------------------ libraries
# usb-serial-for-android probes driver classes reflectively from its prober
# table; a renamed driver is a device that is never recognised.
-keep class com.hoho.android.usbserial.driver.** { *; }

# OkHttp / Okio ship their own consumer rules but warn about optional
# compile-only dependencies that are absent at runtime by design.
-dontwarn okhttp3.**
-dontwarn okio.**
-dontwarn org.conscrypt.**
-dontwarn org.bouncycastle.**
-dontwarn org.openjsse.**

# Retrofit keeps generic signatures for its return types.
-keepattributes Signature, InnerClasses, EnclosingMethod
-keepattributes RuntimeVisibleAnnotations, RuntimeVisibleParameterAnnotations

# --------------------------------------------------------------- misc
# Keep line numbers so a release stack trace is still readable, but hide the
# original source file name.
-keepattributes SourceFile,LineNumberTable
-renamesourcefileattribute SourceFile
