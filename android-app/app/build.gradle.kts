plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.aura.agent"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.aura.agent"
        // The CT45P-X0N ships Android 11; UWB APIs (Android 12+) are accessed
        // reflectively and degrade gracefully when absent.
        minSdk = 30
        targetSdk = 34
        versionCode = 600
        versionName = "6.0.0"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"

        externalNativeBuild {
            cmake {
                cppFlags += listOf("-std=c++17", "-O3", "-fno-exceptions", "-fno-rtti")
                // The CT45P is arm64; armeabi-v7a is kept for older accessories.
                abiFilters += listOf("arm64-v8a", "armeabi-v7a")
            }
        }
        ndk {
            abiFilters += listOf("arm64-v8a", "armeabi-v7a")
        }
    }

    externalNativeBuild {
        cmake {
            path = file("src/main/cpp/CMakeLists.txt")
            version = "3.22.1"
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }
        debug {
            isMinifyEnabled = false
            applicationIdSuffix = ".debug"
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        viewBinding = true
        buildConfig = true
    }

    packaging {
        resources.excludes += setOf("META-INF/AL2.0", "META-INF/LGPL2.1")
        // The GGUF model is already compressed; re-packing it wastes build time
        // and prevents mmap-ing it directly from the APK at runtime.
        jniLibs.useLegacyPackaging = false
    }

    androidResources {
        noCompress += setOf("gguf", "onnx")
    }

    testOptions {
        unitTests.isReturnDefaultValues = true
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    implementation("androidx.constraintlayout:constraintlayout:2.1.4")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.4")
    implementation("androidx.lifecycle:lifecycle-service:2.8.4")
    implementation("androidx.lifecycle:lifecycle-viewmodel-ktx:2.8.4")
    implementation("androidx.fragment:fragment-ktx:1.8.2")
    // Required by MainActivity's tab pager. Missing this is a hard build error;
    // it is NOT transitively provided by material or appcompat.
    implementation("androidx.viewpager2:viewpager2:1.1.0")
    implementation("androidx.preference:preference-ktx:1.2.1")
    implementation("androidx.webkit:webkit:1.11.0")

    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")

    // USB serial for LiDAR / mmWave / SDR over the CT45P's USB-C host port
    implementation("com.github.mik3y:usb-serial-for-android:3.7.0")

    // REST + WebSocket to the edge agent
    implementation("com.squareup.retrofit2:retrofit:2.11.0")
    implementation("com.squareup.retrofit2:converter-gson:2.11.0")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("com.squareup.okhttp3:logging-interceptor:4.12.0")

    // UWB (Android 12+; guarded at runtime)
    implementation("androidx.core.uwb:uwb:1.0.0-alpha08")

    // Encrypted preferences for the API token / WireGuard key material
    implementation("androidx.security:security-crypto:1.1.0-alpha06")

    testImplementation("junit:junit:4.13.2")
    testImplementation("org.robolectric:robolectric:4.12.2")
    testImplementation("org.jetbrains.kotlinx:kotlinx-coroutines-test:1.8.1")
    androidTestImplementation("androidx.test.ext:junit:1.2.1")
    androidTestImplementation("androidx.test.espresso:espresso-core:3.6.1")
}
