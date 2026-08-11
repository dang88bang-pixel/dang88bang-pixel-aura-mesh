# SwarmRadar – ProGuard-Regeln (Basis)
# Hilt / Dagger
-keep class dagger.** { *; }
-keep class javax.inject.** { *; }
-keep class * extends dagger.hilt.android.internal.managers.ViewComponentManager$FragmentContextWrapper

# EJML (Matrizen-Math) – keine Reflection, kann obfuscated bleiben
-keep class org.ejml.** { *; }

# Paho MQTT
-keep class org.eclipse.paho.** { *; }

# Sceneform / ARCore
-keep class com.google.ar.** { *; }
-keep class com.google.ar.sceneform.** { *; }
-dontwarn com.google.ar.**

# USB-Serial
-keep class com.hoho.android.usbserial.** { *; }

# Allgemein
-keepattributes *Annotation*
-keepclassmembers class * { @androidx.annotation.Keep *; }
