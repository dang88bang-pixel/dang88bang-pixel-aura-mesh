# Building the Android app

The Android tier is the one part of this project that was **never assembled**
during development — the authoring environment had no Android SDK and no route
to `dl.google.com`. This document is what you need to close that gap, plus an
honest list of where problems are most likely to surface.

## What *has* been verified

| Layer | How | Result |
|---|---|---|
| Native core (`aura_core.cpp`) | `g++` on the host, plus ASan/UBSan | **618 assertions pass** |
| Pure Kotlin logic (audit chain, UWB geometry, vitals) | `kotlinc` + host JVM (`tools/run-kotlin-tests.sh`) | **126 checks pass** |
| Manifest ↔ classes/resources | static gates in CI (`android-static`) | clean |
| Imports ↔ Gradle dependencies | `tools/check-android-deps.py` | clean |
| USB ids ↔ registry and code | `tools/check-usb-ids.py` | 7 ids valid |
| `getString` ↔ format specifiers | `tools/check-string-formats.py` | clean |
| Interactive views ↔ code | `tools/check-dead-ui.py` | 17 bound |
| R8 keep rules ↔ JNI/inflated classes | `tools/check-proguard-rules.py` | 19 rules |
| JNI symbols ↔ `external fun` | `tools/check-jni-symbols.py`, both directions | **28/28 matched** |
| `aura_jni.cpp`, `llama_bridge.cpp` | compiled with g++ against a stub `jni.h` | build clean |

## What is unverified

Anything that needs the SDK: resource compilation (AAPT2), the manifest merger,
Kotlin against the real `android.jar`, JNI symbol linkage, and every class that
touches `Context`, `SensorManager`, `BluetoothLeScanner`, `SQLiteOpenHelper`,
`VpnService` or `WebView`.

---

## Fastest route: let GitHub build it

No Android SDK required on your machine. `ci/github-actions-apk.yml` builds an
installable APK on a GitHub runner, which has the SDK preinstalled and, unlike
the environment this project was authored in, unrestricted network access.

```bash
mkdir -p .github/workflows
git mv ci/github-actions-apk.yml .github/workflows/apk.yml
git commit -m "ci: enable the APK build" && git push
```

Then **Actions → Build APK → Run workflow**, and download the
`aura-agent-debug-apk` artifact. Full details, including release signing, in
[`ci/README.md`](../ci/README.md).

This has to be done by hand once: the GitHub App used for this branch lacks
the `workflows` permission, so a commit that adds `.github/workflows/` is
rejected by the remote.

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

1. **`usb-serial-for-android` wiring.** `UsbSerialTransport` sets up permission
   and device discovery but leaves the concrete `UsbSerialPort#open` to the
   driver implementation — the streams are never assigned. Expect to finish this
   against real hardware; the parsers it feeds are already tested (see
   `test_lidar_packet_parser_decodes_legacy_nodes`).

2. **UWB on Android 11.** The CT45P-X0N ships API 30; `androidx.core.uwb`
   requires 31+. The dependency is declared but must be reached reflectively or
   behind a `Build.VERSION` guard, otherwise it is a runtime crash on the actual
   target device.

3. **`VpnService` is single-instance.** `GatekeeperVpnService` and any WireGuard
   tunnel are mutually exclusive per app. Choose one at provisioning time.

4. **Foreground-service types.** `location|connectedDevice` on Android 14
   requires the matching runtime permissions to be granted *before*
   `startForeground`, or the service is killed with a `SecurityException`.

### Resolved since the first draft

Two items that used to head this list are now closed:

* **`llama_bridge` missing** — implemented as a *separate* optional library
  (`llama_bridge.cpp`). It builds by default in **stub mode**, which provides
  the four JNI symbols and returns an honest "not compiled in" message. Without
  a stub, every `LLMService` call throws `UnsatisfiedLinkError`, which at the
  call site is indistinguishable from a genuine crash. For the real backend:

  ```bash
  git submodule add https://github.com/ggerganov/llama.cpp \
      android-app/app/src/main/cpp/vendor/llama.cpp
  # then configure the NDK build with -DAURA_WITH_LLAMA=ON
  ```

  The sensor pipeline never depends on it: `libaura_core.so` and
  `libllama_bridge.so` are separate targets, so a device with no model boots
  and maps normally.

* **JNI symbol names** — now machine-checked in both directions by
  `tools/check-jni-symbols.py`, which parses the *enclosing class* (so a nested
  `object NativeEngine` mangles correctly) and compares against `nm` output or
  the C++ sources. It found a real gap on first run: the C++ exported
  `NativePassiveRadar_nativeProcess`/`nativeCfar` but no such Kotlin class
  existed, leaving the whole radar path unreachable from the app. The class is
  now written.

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

## Checking the JNI layer

```bash
tools/check-jni-symbols.py --source-only          # no build required
# or, against real libraries:
tools/check-jni-symbols.py --library build/.../libaura_core.so \
                           --library build/.../libllama_bridge.so
```

Run this after **any** rename of a class or package under `com.aura.agent`.
A mismatch produces no build error whatsoever — only a crash on the device the
first time the feature is used.
