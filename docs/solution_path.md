# Lösungsweg: APK-Build und die restlichen offenen Punkte

**Stand:** 2026-08-14 · **Auftrag:** „suche Lösungsweg" für die offenen Punkte
aus [`docs/INVENTUR.md`](INVENTUR.md) — v. a. den APK-Build (Punkt 1) und die
CI-Workflow-Aktivierung.

**Kurzfassung:** Der einzige saubere Weg zum ersten APK-Build ist
**GitHub Actions**. Ein lokaler Build ist in dieser Umgebung nachweislich
unmöglich (alle SDK-/Maven-/Gradle-Quellen sind blockiert, siehe §2/§3). Die
Aktivierung der Workflows ist durch eine fehlende `workflows`-Permission des
Bots blockiert und wird über **zwei Wege** erreicht: die GitHub-Web-UI
(§4, keine Permission nötig) oder das Erteilen der Permission (§5, danach
übernimmt der Agent). Erwartung beim ersten Lauf: möglicherweise echte
Compiler-Fehler — das Projekt wurde noch nie kompiliert, die Doku rechnet
explizit damit („Expect the first run to fail", `ci/README.md`).

---

## 1. Was offen ist

| # | Punkt | Status vor diesem Dokument |
|---|---|---|
| 1 | **APK bauen** (Manifest-Merger, AAPT2, echter Kotlin-Compile, JNI-Link) | ❌ fehlend — kein Android-SDK, keine Netzroute zu `dl.google.com` |
| — | CI-Workflows aktivieren (`ci.yml`, `apk.yml`) | 🟡 blockiert — GitHub-App ohne `workflows`-Permission (Push + API am 14.08.2026 mit 403 abgelehnt) |
| 4 | Hardware-Verifikation aller Sensoren | ❌ — keine Geräte verbunden |
| 5 | Kotlin-Tests für Context-abhängige Klassen (C2) | ❌ teilweise — nur reine Logik host-testbar |
| 9 | GGUF-Modell für den LLM | ❌ bewusst — Side-Load aufs Gerät |
| 10 | WebXR | ❌ bewusst — nicht vorgesehen |

Bereits erledigt (2026-08-14): Passive-Radar-Verdrahtung (Sim-Feed), Voxel-
Chunk-Writer, Doku-Zahlen, Visualizer-Bundle (26 MB in den Assets), 9 Kotlin-
Host-Suiten (286 Checks).

---

## 2. Netzwerk-Analyse dieser Umgebung (am 14.08.2026 verifiziert)

| Quelle | HTTP | Bedeutung |
|---|---|---|
| `github.com`, `api.github.com`, `codeload.github.com` | ✅ 200 | HTML, REST-API, **Quell-Tarballs** |
| `registry.npmjs.org`, `files.pythonhosted.org` / PyPI | ✅ 200 | npm- und Python-Pakete (kotlinc + JRE via PyPI funktionieren) |
| `raw.githubusercontent.com`, `objects.githubusercontent.com`, `release-assets.githubusercontent.com` | 🔒 000 | **GitHub-Release-Binaries und Raw-Dateien sind blockiert** |
| `dl.google.com`, `maven.google.com`, `storage.googleapis.com` | 🔒 000 | Android SDK + Google Maven |
| Maven Central (`repo.maven.apache.org`, `repo1`, `central.sonatype.com`, `search.maven.org`) | 🔒 000 | AGP-, androidx-, kotlinx-Jars |
| Gradle (`services.gradle.org`, `plugins.gradle.org`, `downloads.gradle.org`) | 🔒 000 | Gradle-Distribution + Plugin-Portal |
| CN-Mirrors (Tsinghua, Tencent, Huawei, Aliyun: `AndroidSDK/`, Maven, Gradle) | 🔒 000 | auch alle Spiegel blockiert |
| Container (`ghcr.io`, Docker Hub; Docker nicht installiert) | 🔒 000 | keine Container-Route |
| `googlesource.com` (AOSP), `huggingface.co`, `jitpack.io`, apt | 🔒 000 | keine weiteren Quellen |

**Fazit:** Erreichbar sind nur GitHub (ohne Binaries), PyPI und npm. Damit
fehlen **alle** Binärquellen für SDK, Build-Tools, Gradle und Maven-
Artefakte.

---

## 3. Warum ein lokaler Build hier nicht möglich ist

### 3.1 Gradle/AGP-Build — nicht möglich
Für `./gradlew assembleDebug` fehlen: die Gradle-Distribution
(`services.gradle.org`), das Android SDK (`dl.google.com`), AGP und alle
androidx-/kotlinx-Abhängigkeiten (Google Maven / Maven Central). Jede dieser
Quellen ist blockiert; auch die üblichen Mirrors sind blockiert.

### 3.2 Hand-gerollter Build (kotlinc + aapt2 + d8 + apksigner) — geprüft und verworfen
| Baustein | Verfügbarkeit | Ergebnis |
|---|---|---|
| `kotlinc` + JRE | ✅ PyPI (`kotlin-jupyter-kernel`, `jdk4py`) | funktioniert (286 Host-Checks laufen) |
| `android.jar` (API 34) | ✅ `Sable/android-platforms` via codeload (26 MB) | erreichbar |
| `aapt2` (Ressourcen-Compiler) | ❌ kein offizielles Binary; PyPI-Paket `aapt2 0.2.1` ist ein ungeprüfter Wrapper | unbrauchbar |
| `d8`/`dx` (Dexer) | ❌ nur in Build-Tools (blockiert); `enjarify` nur als Quelltext | unbrauchbar |
| `apksigner` v2 | ❌ nirgends verfügbar; **minSdk 30 verlangt v2/v3**, v1 (jarsigner) wird auf API 30 abgelehnt | unbrauchbar |
| androidx-/kotlinx-Jars | ❌ Maven blockiert | Compile der App unmöglich |

Selbst wenn die Werkzeuge zusammenkämen: Der hand-gerollte Build würde **nicht
die AGP-Pipeline validieren** (Manifest-Merger, AAPT2-Ressourcen, R8/ProGuard,
Desugaring, JNI-Link) — also genau das nicht prüfen, was unverifiziert ist.
Er wäre eine riskante Parallelinfrastruktur, die ein falsches „grün" liefern
kann. **Verworfen.**

---

## 4. Lösungsweg A — Aktivierung über die GitHub-Web-UI (empfohlen, ~5 Minuten, keine Permission nötig)

Der Web-Editor ist **nicht** an die `workflows`-Permission der GitHub-App
gebunden. Schritte:

1. <https://github.com/dang88bang-pixel/dang88bang-pixel-aura-mesh> öffnen →
   Tab **Actions**.
2. **„New workflow"** → **„set up a workflow yourself"** → Dateiname
   `.github/workflows/apk.yml`.
3. Den kompletten Inhalt von [`ci/github-actions-apk.yml`](../ci/github-actions-apk.yml)
   einfügen (Datei im Repo, aktuell gepflegt) → **Commit changes**.
4. Optional dasselbe mit [`ci/github-actions-ci.yml`](../ci/github-actions-ci.yml)
   als `.github/workflows/ci.yml` (Tests + Gates; läuft ohne SDK).
5. Der `apk.yml`-Workflow läuft automatisch bei jedem Push auf `android-app/**`
   und lässt sich zusätzlich manuell starten: **Actions → Build APK → Run
   workflow** (Häkchen „build_release" setzt ein zusätzliches, R8-geshrt
   Release-APK).
6. Artefakt **`aura-agent-debug-apk`** aus dem Run herunterladen →
   `adb install -r aura-agent-debug.apk` oder per Side-Load auf den CT45P.

---

## 5. Lösungsweg B — Permission erteilen (dann übernimmt der Agent)

1. Repo: **Settings → Integrations → GitHub Apps → „Arena AI Coding Agent" →
   Configure**.
2. **Repository permissions → Workflows: Read and write** → Save.
3. Danach genügt eine Rückmeldung: Der Agent pusht die bereits vorbereiteten,
   lokal untracked liegenden Dateien `.github/workflows/{ci,apk}.yml`, startet
   den Build und fixt die Fehler aus dem ersten Lauf.

---

## 6. Was der erste Workflow-Lauf tut und was zu erwarten ist

| Schritt im Workflow | Erwartung |
|---|---|
| JDK 17 + Android SDK (Runner) | läuft, kein Netzproblem |
| Gradle-Wrapper erzeugen (8.7) | läuft (Wrapper-JAR wird bewusst nicht committet) |
| `npm ci` + `tools/bundle-visualizer.sh` | funktioniert — hier bereits lokal verifiziert (26 MB, 2 057 Dateien) |
| 6 statische Gates | **grün** — hier heute verifiziert |
| `:app:lintDebug` | `continue-on-error` (informativ) |
| **`:app:assembleDebug`** | **erster echter Compile.** Die Doku sagt es voraus: `ci/README.md` → „Expect the first run to fail". Fehler werden Schritt für Schritt gefixt; jede Fix-Klasse ist eine, die die statischen Gates nicht finden konnten |
| JNI-Check in den gepackten `.so` | muss 28/28 `Java_com_aura*`-Symbole in beiden ABIs zeigen |
| Release (optional, R8) | R8 ist der wahrscheinlichste Ort für „baut, crasht auf dem Gerät" — danach ProGuard-Regeln prüfen |

---

## 7. Danach: die restlichen offenen Punkte

| # | Punkt | Lösungsweg |
|---|---|---|
| 4 | Hardware-Verifikation | Erst nach dem APK-Build: APK auf den CT45P (oder ein Testgerät) installieren; LiDAR, mmWave, UWB, SDR, Thermal nacheinander anschließen. DWM3000: Aura-Anchor-Protokoll gegen echtes Board oder eigene Anchor-Firmware testen (`docs/uwb_anchor_protocol.md`) |
| 5 | Context-abhängige Tests (C2) | Nach dem ersten grünen Build: **Robolectric**-Suite im CI ergänzen (läuft auf dem Runner, Jars kommen aus dessen offenem Netz; ersetzt den Emulator-Bedarf für die meisten `Context`-Fälle) |
| 9 | GGUF-Modell | Auf dem Gerät bzw. in einem Netz mit HuggingFace-Zugang laden und side-loaden (`adb push … /sdcard/Android/data/com.aura.agent/files/`); nie ins Repo |
| 10 | WebXR | bewusst nicht vorgesehen (CT45P, kein Headset) — kein Handlungsbedarf |

---

## 8. Verifikations-Checkliste nach dem ersten APK-Run

- [ ] `aura-agent-debug-apk`-Artefakt vorhanden, Größe dokumentiert
- [ ] `lib/arm64-v8a/libaura_core.so` (+ `libllama_bridge.so`) im APK, JNI-Symbole ≥ 28
- [ ] `assets/visualizer/index.html` im APK (Map-Tab zeigt keine Platzhalter)
- [ ] Installation auf dem CT45P erfolgreich, App startet, Service läuft
- [ ] `tools/run-kotlin-tests.sh` + 300 Python-Tests + 634 native Checks weiterhin grün
- [ ] Alle Punkte in `docs/INVENTUR.md` entsprechend aktualisieren
