# Completion plan — what is still missing

An audit of what is actually wired up, as opposed to what exists as a file.
Compiled 2026-08-13 by cross-referencing the class inventory, the layout ids,
the manifest and the spec against the code that uses them.

The headline: **the edge agent and the web visualiser are complete and
verified. The Android tier has all its parts written but most of them are not
connected to anything.** Five subsystems are never instantiated, and 23 of 27
layout ids are never read by code — the three secondary fragments inflate XML
and stop there.

## How each item was established

| evidence | command |
|---|---|
| never-instantiated classes | `grep -rn "ClassName(" --include=*.kt` minus the declaration |
| dead layout ids | `comm -23` of ids in `res/layout/*.xml` vs `R.id.*` in Kotlin |
| endpoint coverage | each spec route grepped in `api.py`, then curled live |
| button coverage | every `id="btn-*"` in `index.html` grepped in `src/*.js` |

---

## A. Android — subsystems written but never reached

| # | item | evidence | consequence | effort |
|---|---|---|---|---|
| **A1** | `LiveViewFragment` shows no data | 8 ids in `fragment_live.xml`, none read | The main tab is permanently empty. Highest user-visible impact. | M |
| **A2** | `ScenarioFragment` inert | `scenario_spinner/start/stop/progress/metrics` unused | Scenarios cannot be run from the device at all. | M |
| **A3** | `SettingsFragment` inert | `server_url`, `verify_audit`, `manage_tokens`, `*_status` unused | Agent URL is only settable via SharedPreferences; audit cannot be verified in the field. | M |
| **A4** | `AgentApiClient` never constructed | no call sites | The device cannot talk to the edge agent. Blocks A1–A3. | S |
| **A5** | `NativeRti` never constructed | no call sites | The RTI imaging path is unreachable from the app. | S |
| **A6** | `NativePassiveRadar` never constructed | no call sites | Same, for passive radar. | S |
| **A7** | `LLMService` never constructed | no call sites | The offline assistant is unreachable. | M |
| **A8** | `MainActivity` has a `toolbar` id it never binds | `toolbar` unused | Cosmetic. | XS |

## B. Correctness risks (from `docs/android_build.md`)

| # | item | status |
|---|---|---|
| **B1** | USB-serial wiring | **done** (commit `0c2906c`) |
| **B2** | UWB on API 30 | **done** (reflective path, `0c2906c`) |
| **B3** | `VpnService` single-instance | documented; needs a provisioning-time guard so both cannot be enabled |
| **B4** | Foreground-service types on Android 14 | **unverified** — permissions must be granted *before* `startForeground` or the service is killed |

## C. Verification gaps

| # | item | note |
|---|---|---|
| **C1** | No Android build possible | `dl.google.com` + Maven Central blocked; all mirrors tried and unreachable. Static gates only. |
| **C2** | Kotlin coverage is partial | Only `CausalValidator` and `UwbGeometry` are host-testable. Anything touching `Context` needs an emulator. |
| **C3** | 7 claims still `NEEDS SOURCE` | Mostly the DWM3000 protocol — project-specific, not publicly verifiable. |

## D. Confirmed complete (no action)

- **Edge agent** — all 6 spec routes live, 38 routes total, 162 tests.
- **Web visualiser** — all 10 buttons wired; `export/gltf` and `export/json`
  both return 200 against the running agent.
- **Native core** — 618 checks; JNI 28/28 both directions.
- **Avatars, smoke, halos, point cloud, timeline** — present in `SceneManager`.
- **WebXR** — absent, and deliberately: it needs HTTPS plus a headset, neither
  of which applies to a handheld CT45P. Recorded here so it is not mistaken
  for an oversight.

---

## Order of work

Dependency-driven, most user-visible first:

1. **A4** `AgentApiClient` wiring — unblocks A1–A3.
2. **A1** Live view — the tab users see first.
3. **A2** Scenario control.
4. **A3** Settings + audit verification.
5. **A5/A6** RTI and passive radar entry points.
6. **B4** foreground-service permission ordering.
7. **B3** VPN mutual-exclusion guard.
8. **A7** LLM assistant UI.

Each step: implement → extend the static gates where the failure would
otherwise be silent → run all gates → commit → push.
