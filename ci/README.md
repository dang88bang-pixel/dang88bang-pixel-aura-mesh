# CI pipelines

Two workflow definitions live here:

| file | purpose |
|---|---|
| `github-actions-ci.yml` | tests and static gates — no Android SDK needed |
| `github-actions-apk.yml` | **builds an installable APK** and uploads it as an artifact |

## Why they are parked in `ci/` and not `.github/workflows/`

The GitHub App used to push this branch does not hold the `workflows`
permission, so any commit touching `.github/workflows/` is rejected by the
remote:

```
! [remote rejected] ... refusing to allow a GitHub App to create or update
  workflow `.github/workflows/...` without `workflows` permission
```

This was last confirmed on 2026-08-13. A later session may have the
permission; activating them is still the one step that can be rejected by
the remote, and it takes about ten seconds. The copies in `ci/` stay so a
rejected push does not lose the definitions.

## Activation

```bash
mkdir -p .github/workflows
git mv ci/github-actions-ci.yml  .github/workflows/ci.yml
git mv ci/github-actions-apk.yml .github/workflows/apk.yml
git commit -m "ci: enable GitHub Actions"
git push
```

You can also create the two files through the GitHub web UI (**Actions → new
workflow**) and paste the contents — the web editor is not subject to the App
permission.

## Getting the APK

Once `apk.yml` is in place:

1. **Actions** → **Build APK** → **Run workflow**, or just push to
   `android-app/**`.
2. When the run finishes, the APK is under **Artifacts** →
   `aura-agent-debug-apk` (retained 30 days).
3. Install with `adb install -r aura-agent-debug.apk`, or copy it to the CT45P
   and open it (needs "install unknown apps" enabled).

The debug build has `applicationIdSuffix = ".debug"`, so it installs alongside
a release build rather than replacing it.

### Release APK

Tick **build_release** when dispatching manually. That runs R8, which is the
step most likely to produce a build that works on CI and breaks on the device
— it renames the classes holding the JNI bindings unless `proguard-rules.pro`
keeps them. `tools/check-proguard-rules.py` guards exactly that, but the
authoritative test is running the release APK on hardware.

The artifact is **unsigned**. To install it you need to sign it yourself:

```bash
# one-off: create a keystore
keytool -genkey -v -keystore aura.jks -keyalg RSA -keysize 2048 \
        -validity 10000 -alias aura

# sign and align
$ANDROID_HOME/build-tools/34.0.0/zipalign -v -p 4 \
        app-release-unsigned.apk app-release-aligned.apk
$ANDROID_HOME/build-tools/34.0.0/apksigner sign \
        --ks aura.jks --out aura-release.apk app-release-aligned.apk
```

Signing is deliberately **not** automated here: that would mean committing a
keystore or storing one in repository secrets, and a signing key that CI can
use is a signing key an attacker who compromises CI can use. For a fleet
deployment, sign from a controlled machine or use Play App Signing.

## What the APK workflow does

| step | why it is there |
|---|---|
| Generate the Gradle wrapper | `gradle-wrapper.jar` is deliberately not committed (see the header of `android-app/gradlew`); it is generated from the runner's own Gradle |
| Bundle the visualiser | `MapFragment` loads `file:///android_asset/visualizer/index.html`; without this the tab shows its "bundle not embedded" placeholder. 26 MB, gitignored, built from `web-visualizer/` so the two views cannot drift |
| Static gates | the same six checks that run locally, before spending build minutes |
| Assemble debug | the actual compile |
| Verify packaged `.so` | confirms the JNI symbols verified in *source* are present in the packaged libraries, for both ABIs |

### Pinned versions, and why

`ndkVersion` and the CMake version are pinned in `app/build.gradle.kts` and
overridable via `AURA_NDK_VERSION` / `AURA_CMAKE_VERSION`.

NDK **27.3.13750724** is the default on `ubuntu-24.04` runners. NDK 26 was
*removed* from those images in January 2026, so pinning the older LTS would
break CI rather than stabilise it. Same reasoning for CMake: the images ship
3.31.5 and 4.1.2, and pinning 3.22.1 fails on a machine that does not have it
even though `CMakeLists.txt` only *requires* 3.22.

## Expect the first run to fail

This matters more than it sounds. **The Android tier has never been
compiled.** There is no Android SDK in the environment this project was
written in and no network route to `dl.google.com`, so every Android-side
guarantee here comes from static analysis:

- 126 host-JVM Kotlin tests covering the pure logic (audit chain, UWB geometry,
  vitals estimation)
- six static gates covering JNI symbols, dependencies, USB ids, string
  formats, dead UI and R8 keep rules

Those catch the mistakes that are *checkable* without a compiler. They do not
catch type errors in code touching `Context`, `SensorManager` or
`SQLiteOpenHelper`, because nothing here can type-check against `android.jar`.

The APK workflow is the first thing that will. Treat the first few runs as
part of the build, not as a regression: fix what it reports, and each fix is
one the static gates could never have found.

## What the test workflow runs

| Job | Purpose |
|---|---|
| `edge-agent` | ruff + the 162 Python tests |
| `native-core` | the 618 C++ assertions, then a rebuild under ASan/UBSan |
| `kotlin-logic` | the host-JVM Kotlin suites |
| `android-static` | manifest, dependency, USB, string, UI and R8 gates |
| `visualizer` | syntax-checks every module and smoke-tests the server |
| `integration` | starts the agent and visualiser together and verifies health, proxied state, glTF export and audit verification |

All of these run without hardware — the sensor drivers fall back to
simulators, which is why the stack is CI-testable at all.
