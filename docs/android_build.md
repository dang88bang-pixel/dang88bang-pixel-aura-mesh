# Building the Android app

The Android tier is the one part of this project that was **never assembled**
during development — the authoring environment had no Android SDK and no route
to `dl.google.com`. This document is what you need to close that gap, plus an
honest list of where problems are most likely to surface.

## What *has* been verified

| Layer | How | Result |
|---|---|---|
| Native core (`aura_core.cpp`) | `g++` on the host, plus ASan/UBSan | **618 assertions pass** |
| `CausalValidator.kt` | `kotlinc` + host JVM (`tools/run-kotlin-tests.sh`) | **44 checks pass** |
| Manifest ↔ classes/resources | static gates in CI (`android-static`) | clean |
| Imports ↔ Gradle dependencies | static gate in CI | clean |

## What is unverified

Anything that needs the SDK: resource compilation (AAPT2), the manifest merger,
Kotlin against the real `android.jar`, JNI symbol linkage, and every class that
touches `Context`, `SensorManager`, `BluetoothLeScanner`, `SQLiteOpenHelper`,
`VpnService` or `WebView`.

---

## Prerequisites

* JDK 17 (Android Gradle Plugin 8.5 requires 17, not 21)
* Android SDK Platform 34, Build-Tools 34.0.0
* NDK r26d or newer (for `arm64-v8a`)
* CMake 3.22.1
* Gradle 8.7 — the wrapper JAR is **not** committed, see below

```bash
sdkmanager "platforms;android-34" "build-tools;34.0.0" \
           "ndk;26.3.11579264" "cmake;3.22.1"
```

## First build

```bash
cd android-app

# The wrapper JAR is deliberately absent from git. Generate it from a trusted
# local Gradle rather than trusting a committed binary:
gradle wrapper --gradle-version 8.7

echo "sdk.dir=$ANDROID_HOME" > local.properties

./gradlew assembleDebug
adb install app/build/outputs/apk/debug/app-debug.apk
```

Android Studio does all of the above on first project open.

### Why the wrapper JAR is not committed

`gradle-wrapper.jar` is a 60 kB opaque binary that executes on every build.
Committing one you cannot verify is the exact supply-chain shape that has burned
projects before, and this environment could not reach `services.gradle.org` to
obtain an authentic copy. `gradlew` therefore fails with an explanatory message
rather than a cryptic Java error. Verify any Gradle distribution you use against
<https://gradle.org/release-checksums/>.

---

## Before packaging a release

### 1. Bundle the 3D visualiser

`MapFragment` loads `file:///android_asset/visualizer/index.html`. That bundle
is generated from `web-visualizer/`, not maintained separately — two renderers
would let the semantic colour code drift between the handheld and the command
post.

```bash
(cd web-visualizer && npm install)
tools/bundle-visualizer.sh          # ~26 MB into app/src/main/assets/
```

The directory is gitignored. A debug build without it installs and runs fine;
the map tab shows an explanatory placeholder instead of a blank WebView.

### 2. Side-load the LLM model (optional)

Do not commit or bundle a GGUF: 1–2.4 GB blows past the Play limit and makes
every checkout slow.

```bash
adb push qwen2.5-1.5b-instruct-q4_k_m.gguf \
  /sdcard/Android/data/com.aura.agent/files/
```

`LLMService` searches `filesDir`, then `getExternalFilesDir()`, then `assets`.
Default is Qwen2.5-1.5B (10–16 t/s on a QCS4290); Phi-3-mini is opt-in at
3–6 t/s — see `performance_targets.md#4`.

---

## Where problems are most likely

Ranked by how much I would bet on each one failing first.

1. **`llama_bridge` does not exist.** `LLMService` declares `external` methods
   and loads a library that this repository does not contain — llama.cpp has to
   be vendored and wired into CMake. The `System.loadLibrary` call is already
   wrapped in `runCatching`, so the app starts without it and the assistant is
   simply unavailable. Everything else keeps working.

2. **JNI symbol names.** `aura_jni.cpp` hard-codes
   `Java_com_aura_agent_fusion_NativeEkf_nativeCreate` and friends. Any package
   or class rename silently produces `UnsatisfiedLinkError` at runtime, not a
   build error. If you move a class, regenerate with `javah`/`javac -h`.

3. **`usb-serial-for-android` wiring.** `UsbSerialTransport` sets up permission
   and device discovery but leaves the concrete `UsbSerialPort#open` to the
   driver implementation — the streams are never assigned. Expect to finish this
   against real hardware; the parsers it feeds are already tested (see
   `test_lidar_packet_parser_decodes_legacy_nodes`).

4. **UWB on Android 11.** The CT45P-X0N ships API 30; `androidx.core.uwb`
   requires 31+. The dependency is declared but must be reached reflectively or
   behind a `Build.VERSION` guard, otherwise it is a runtime crash on the actual
   target device.

5. **`VpnService` is single-instance.** `GatekeeperVpnService` and any WireGuard
   tunnel are mutually exclusive per app. Choose one at provisioning time.

6. **Foreground-service types.** `location|connectedDevice` on Android 14
   requires the matching runtime permissions to be granted *before*
   `startForeground`, or the service is killed with a `SecurityException`.

---

## Running the host-side Kotlin tests

No SDK required — this is what CI runs:

```bash
tools/run-kotlin-tests.sh
```

It regenerates the cross-platform digests from the Python reference, compiles
`CausalValidator.kt` against a small `org.json` shim, and runs 44 checks.

The important one is `crossPlatformHashMatchesPython`. It caught two bugs that
would have been invisible until a court asked why a CT45P's audit log did not
verify on the server:

* Kotlin rendered `1000.0` as `1000` while Python emits `1000.0`. Every
  timestamp is a float, so **every** entry hashed differently.
* `(1.001 * 1000.0).toLong()` floors to `1000` because IEEE-754 gives
  `1000.9999999999999`, so any entry not on a whole second broke after a
  round-trip.

If you change `aura/audit.py` or `CausalValidator.kt`, run this before pushing.
