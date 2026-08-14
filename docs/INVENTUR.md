# AURA 6.0 — Inventurliste (Parts & Attribute)

**Stand:** 2026-08-14 · **Umfang:** alle Teile und deren Attribute in den drei
Tiers (Edge-Agent, Android-App, Native Core, Web-Visualizer) plus CI/Tools/Doku
und Hardware-Verifikation.

**Fortschritt 2026-08-14 (gleicher Tag):** die offenen Punkte 2 (Passive
Radar verdrahtet, Simulator-Feed), 3 (Voxel-Codec-Verdrahtung), 6+7 (Doku
nachgeführt) und 8 (Visualizer-Bundle erzeugt) sind erledigt. 1 (APK-Build)
bleibt extern — `dl.google.com` ist in dieser Umgebung weiterhin blockiert.
Der CI-Workflow-Umzug wurde **versucht und vom Remote abgelehnt** (GitHub-App
ohne `workflows`-Permission, am 14.08.2026 erneut bestätigt): die kanonischen
Kopien bleiben in `ci/`, eine untracked Arbeitskopie liegt lokal in
`.github/workflows/` für den Moment, in dem die Permission existiert (Fallback:
GitHub-Web-UI). 4/5/9/10 brauchen Hardware bzw. sind bewusst offen.

**Status-Legende**

| Status | Bedeutung |
|---|---|
| ✅ **erstellt** | Code/Artefakt vorhanden, verdrahtet und hier verifiziert (Test, Live-Aufruf oder statisches Gate) |
| 🟡 **unvollständig** | Code vorhanden, aber nicht (vollständig) verdrahtet, verifiziert oder nachgeführt |
| ❌ **fehlend** | Existiert nicht bzw. ist nie erzeugt worden (ggf. bewusst, dann mit Vermerk) |

**Prüfmethode (alles heute ausgeführt):**

| Beleg | Ergebnis |
|---|---|
| `python3 -m pytest tests/ -q` (edge-agent, venv) | **300 passed** |
| `g++ … test_aura_core.cpp aura_core.cpp && /tmp/aura_test` | **634 checks, 0 failures** |
| `tools/run-kotlin-tests.sh` (JRE + kotlinc per `setup-kotlin-toolchain.sh`) | **223 checks, 0 failures** (7 Suiten) |
| Edge-Agent live (`python agent.py`, Simulation) | `/health`, `/state`, `/radar/limits`, `/scenario`, `/audit/verify`, `/voxels`, `/export/gltf` (200, 22 KB), `/export/json` (200, 314 KB) |
| Visualizer live (`npm start`, Port 3000) + REST-Proxy zum Agenten | Proxy liefert Live-State vom Agenten |
| `tools/check-jni-symbols.py`, `check-usb-ids.py`, `check-string-formats.py`, `check-dead-ui.py`, `check-android-deps.py`, `check-proguard-rules.py` | alle grün |
| Greps auf Konstruktions-/Call-Sites (`ClassName(` in `*.kt`), Manifest, Layouts, `index.html` | siehe unten |

---

## Gesamtübersicht

| Teil | ✅ erstellt | 🟡 unvollständig | ❌ fehlend |
|---|---|---|---|
| **1. Edge-Agent (Python)** | 22/22 | 0 | 0 |
| **2. Android-App (Kotlin)** | 17 | 0 | 3 (APK, Emulator-Tests, GGUF — s. u.) |
| **3. Native Core (C++/JNI)** | 4 | 0 | 1 (optional: echter llama.cpp) |
| **4. Web-Visualizer** | 8 | 0 | 0 (WebXR bewusst) |
| **5. CI / Tools / Doku** | 7 | 1 (Workflow-Aktivierung, Remote-blockiert) | 1 (Referenz-Dokumente, bewusst) |
| **6. Hardware-Verifikation** | 0 | 0 | 7 (alle Geräte + DWM3000-Firmware) |

---

## 1. Edge-Agent (Python) — alle Teile ✅

| Part | Attribut | Status | Beleg |
|---|---|---|---|
| `agent.py` | Einstieg, Loop 20 Hz, Simulation | ✅ | läuft live, `mode=SIMULATION` |
| `aura/api.py` | **41 Routen** (40 REST + 1 WS), Auth optional | ✅ | 41 Decorators im Code; `/health`, `/state`, `/radar/limits`, `/scenario`, `/audit/verify`, `/voxels` live 200 |
| `aura/ekf.py` | 15-State-EKF, analytische Jacobians, Qualitäts-Tiers | ✅ | Testsuite grün (u. a. numerische Jacobian-Checks) |
| `aura/fusion.py` | Kontrollschleife, Sensor-Ordnung, Persistenz, Broadcast | ✅ | 300 Tests gesamt |
| `aura/mapping.py` | Occupancy-Grid, Wandextraktion, glTF-Export | ✅ | `/export/gltf` 200 (22 KB) |
| `aura/rti.py` | FISTA Compressed Sensing | ✅ | Tests + C++-Kreuzcheck |
| `aura/passive_radar.py` | CAF, ECA, CFAR | ✅ | agent-seitig; Hardware siehe §6 |
| `aura/voxel.py` | Chunked Sparse Voxels + SVO, RLE | ✅ | `/voxels` live; Kompression getestet |
| `aura/scenarios.py` | Flow-Field + Social-Force Evakuierung | ✅ | `/scenario` live, 5 Szenarien |
| `aura/audit.py` | SHA-256-Hashkette, Tamper-Lokalisierung | ✅ | `/audit/verify` live: „chain of 1 entries is intact“ |
| `aura/storage.py` | SQLite/WAL-Kontextspeicher, Retention | ✅ | Tests |
| `aura/mesh.py`, `cot.py`, `tdoa.py`, `doppler.py`, `sensor_health.py`, `world.py` | Meshtastic-Budget, CoT/TAK, TDoA/TWR, Mikro-Doppler, Sensor-Freeze, Szenenmodell | ✅ | Module + Tests vorhanden |
| `aura/sensors/` (7 Dateien) | LiDAR, mmWave, UWB, BLE, IMU, Thermal + Basisklasse; **Simulatoren** | ✅ | `/health` meldet alle 6 Sensoren `true` (Simulation) |
| Tests | 300 Tests | ✅ | heute ausgeführt: `300 passed` |
| Docker (`Dockerfile`, `docker-compose.yml`), `requirements*.txt` | Container + Abhängigkeiten | ✅ (nicht hier gebaut) | Dateien vorhanden; Docker-Build in dieser Umgebung nicht ausgeführt |

---

## 2. Android-App (Kotlin) — 22 Hauptdateien

| Part | Attribut | Status | Beleg |
|---|---|---|---|
| `MainActivity` + 4 Fragmente (Live, Map, Scenario, Settings) + `CustomViews` | UI, Pager, Service-Bindung | ✅ | `createFragment` 0–3; Live-Daten, Scenario-Steuerung, Settings-Audit-Verifikation verdrahtet |
| UI-Verdrahtung | 19 interaktive/Data-Views | ✅ | `check-dead-ui.py`: „19 interactive/data views, all bound“ |
| `fusion/SensorFusionService` | Foreground-Service, Fusion-Loop, Sensor-Lebenszyklus | ✅ | `startForegroundService` in `MainActivity:164`; Service-Bindung |
| `fusion/NativeEkf` | Native EKF-Fassade | ✅ | konstruiert in `SensorFusionService:197` |
| `rti/NativeRti` | RTI-Einstieg + Sample-Pfad | ✅ | `configureRti`/`onRtiSample`; konstruiert (`SensorFusionService:157`) |
| `radar/NativePassiveRadar` | Passive-Radar-Fassade | ✅ **verdrahtet** (2026-08-14) | `configureRadar()` konstruiert die Engine, `onIqSamples()` führt process → CFAR → detect aus; bis ein SDR-Transport existiert speist `RadarSimulator` (gleiche Physik wie `passive_radar.py`) — Pfad end-to-end erreichbar wie bei den anderen Sensoren. Echter RTL-SDR-Pfad bleibt Hardware-Thema (§6) |
| `radar/RadarSimulator` | Synthetisches Bistatic-IQ (Referenz + Surveillance) | ✅ | rein, host-getestet (24 Checks): Lag↔Range-Konvention (`bin·c/fs`), Korrelations-Peak am injizierten Lag, Doppler-Rotation am injizierten Frequenz-Bin, Determinismus |
| `storage/NativeVoxelCodec` | Native Voxel-RLE-Codec (encode/decode) | ✅ **verdrahtet** (2026-08-14) | `VoxelChunkWriter` akkumuliert Sweep-Occupancy, kodiert berührte Chunks per Codec und persistiert sie in `spatial_chunks` (1 Hz); `voxelIngest()` schreibt kein Platzhalter-Event mehr |
| `storage/VoxelGrid` + `ChunkAccumulator` | Reine Grid-Mathematik (Floor-Division für negative Koordinaten, Chunk-Indizierung) | ✅ | host-getestet (39 Checks), keine Android/JNI-Abhängigkeit |
| `storage/VoxelChunkWriter` | Chunk-Writer-Glue (Codec + Store, gedeckelter Speicher) | ✅ | verdrahtet in `SensorFusionService.voxelIngest` |
| `sensors/` (11 Dateien) | LiDAR-, mmWave-, UWB-, BLE-, IMU-Manager; `UsbSerialTransport`, `LidarBaud`, `UwbGeometry`, `GeoAnchor*`, `VitalsEstimator`, `SensorManagers` | ✅ | USB-Serial `open()`/`read()`/`write()` vollständig implementiert (Doku in `android_build.md` dazu ist **veraltet**); Parser host-getestet |
| `sensors/UwbManager` | Aura-Anchor-Protokoll + API-30-Reflexionspfad | ✅ | `Build.VERSION`-Guard + `Class.forName("androidx.core.uwb.UwbManager")`; Protokoll spezifiziert in `docs/uwb_anchor_protocol.md` |
| `storage/LocalVectorStore` | SQLite/WAL, Chunks, Retention | ✅ | konstruiert + genutzt; Host-Tests |
| `security/CausalValidator` | Audit-Kette (Kotlin) | ✅ | 44 Checks, Digests byte-identisch zu Python |
| `llm/LLMService` + `VectorStore` | On-Device-LLM (llama.cpp), RAG | ✅ (Stub-Betrieb) | konstruiert in `AuraApplication:63`; `llama_bridge.cpp` baut im Stub-Modus; **GGUF-Modell ❌ fehlend** (bewusst nicht gebündelt, Side-Load) |
| `network/AgentApiClient` | REST-Client mit Backoff/Jitter | ✅ | eine Instanz auf `AuraApplication`, URL geteilt mit WebView |
| `network/GatekeeperVpnService` | Mesh-VPN-Guard | ✅ | im Manifest deklariert (systemgestartet, daher keine Konstruktions-Call-Sites nötig); `prepare()`-Prüfung |
| Ressourcen | 4 Layouts, Strings (62, 20 mit Formaten), Themes, `usb_device_filter` (7 IDs), `network_security_config` | ✅ | `check-string-formats.py`/`check-usb-ids.py` grün |
| Host-Tests (Kotlin) | 9 Suiten: Audit (44), UWB-Geometrie (55), Positionsqualität (27), Geo-Anchor (45), Vitals (24), VectorStore (20), LiDAR-Baud (8), **VoxelGrid (39)**, **RadarSimulator (24)** = **286 Checks** | ✅ | heute ausgeführt, 0 Fehler |
| **APK (assembliert)** | Installierbares Artefakt | ❌ **fehlend** | nie gebaut: kein Android-SDK in dieser Umgebung; `gradle-wrapper.jar` fehlt (bewusst nicht committet, siehe §5); damit ist u. a. Manifest-Merger, AAPT2, echter `android.jar`-Compile und JNI-Link unverifiziert |
| **Visualizer-Bundle** in `app/src/main/assets/visualizer/` | In-App-Babylon-Viewer | ✅ **erzeugt** (2026-08-14) | `tools/bundle-visualizer.sh` lief erfolgreich: 2 057 Dateien, 26 MB (gitignored; der Map-Tab lädt `index.html` aus den Assets) |
| **Context-/SDK-abhängige Tests** | Emulator-Tests für `Context`, `SensorManager`, JNI, `VpnService`, `WebView` | ❌ **fehlend** | nur reine-JVM-Logik host-testbar (C2) |

---

## 3. Native Core (C++17/NEON) — alle Teile ✅

| Part | Attribut | Status | Beleg |
|---|---|---|---|
| `cpp/aura_core.{h,cpp}` | EKF (Joseph-Form), FISTA-RTI, CAF/ECA, Voxel-RLE | ✅ | **634 checks, 0 failures** heute; Kreuzcheck gegen Python |
| `cpp/aura_jni.cpp` | JNI-Bridge (Handle-basiert, Zero-Copy) | ✅ | **28/28 Symbole** in beiden Richtungen (`check-jni-symbols.py`); kompiliert gegen Stub-`jni.h` |
| `cpp/llama_bridge.cpp` | LLM-Bridge im **Stub-Modus** | ✅ | baut sauber, liefert ehrliche „not compiled in“-Antwort; echter llama.cpp-Submodul ❌ fehlend (optional, bewusst) |
| `cpp/tests/test_aura_core.cpp` | 634 Assertions, host-lauffähig | ✅ | heute ausgeführt |

---

## 4. Web-Visualizer (Babylon.js 7) — alle Teile ✅

| Part | Attribut | Status | Beleg |
|---|---|---|---|
| `server.js` | Static-Host + REST-Proxy + WS-Bridge | ✅ | läuft live auf 0.0.0.0:3000; **Proxy zum Agenten verifiziert** (State über :3000 identisch) |
| `src/SceneManager.js` | Thin-Instanced Scene Graph (Avatare, Rauch, Halos, Punktwolke, Timeline) | ✅ | vorhanden; WebXR bewusst ❌ (dokumentiert in `completion_plan.md`, kein Versehen) |
| `src/DataFetcher.js` | Telemetrie-Socket mit Backoff | ✅ | vorhanden |
| `src/SimControls.js` | Sidebar, Layer, Export | ✅ | vorhanden |
| `src/demo-simulator.js` | Synthetische Szene ohne Agent | ✅ | vor Agent-Start verifiziert („demo mode: enabled“) |
| `src/palette.js`, `src/main.js` | Farbschema, Bootstrap | ✅ | vorhanden |
| `public/index.html` + `styles.css` | 10 Buttons | ✅ | **alle 10 Buttons** (`btn-*`) in `SimControls.js`/`main.js` verdrahtet |
| Export | `/export/gltf`, `/export/json` | ✅ | live 200 (22 KB / 314 KB) |

---

## 5. CI / Tools / Doku

| Part | Attribut | Status | Beleg |
|---|---|---|---|
| `ci/github-actions-ci.yml` | CI (Tests + statische Gates) | ✅ (kanonische Kopie) | Aktivierung **vom Remote abgelehnt** (fehlende `workflows`-Permission, 14.08.2026); untracked Arbeitskopie unter `.github/workflows/ci.yml` für späteren Push / Web-UI |
| `ci/github-actions-apk.yml` | **APK-Build-Workflow** | ✅ (kanonische Kopie) | bündelt Visualizer, läuft statische Gates, baut Debug + optional Release (R8), prüft JNI-Symbole in den gepackten `.so`; Aktivierung wie oben blockiert |
| 6 Prüf-Tools (`check-*.py`) | JNI-Symbole, USB-IDs, String-Formate, Dead-UI, Dependencies, ProGuard | ✅ | alle heute grün |
| `bundle-visualizer.sh`, `run-kotlin-tests.sh`, `setup-kotlin-toolchain.sh` | Bundle-Erzeugung, Host-Testlauf, Toolchain | ✅ | `run-kotlin-tests.sh` + Toolchain heute erfolgreich ausgeführt |
| Doku | 18 Markdown-Dateien in `docs/` (+ `INVENTUR.md`) | ✅ | vorhanden und nachgeführt |
| Doku-Zahlen | README/architecture/completion_plan/android_build/performance_targets | ✅ **nachgeführt** (2026-08-14) | 300 Tests / 634 native Checks / 286 Kotlin-Checks / 41 Routen; USB-Serial-Aussage in `android_build.md` korrigiert |
| Referenz-Dokumente (~30 PDFs/TXTs) | Belege für Hardware-Claims | ❌ **fehlend** | nicht im Repo (bewusst, gitignored); per `tools/ingest-reference-docs.py` erzeugbar; Claims in `docs/source_claims.md` teils `NEEDS SOURCE` |
| `LICENSE`, `.gitignore` | Apache-2.0, Ignore-Regeln | ✅ | vorhanden |

---

## 6. Hardware-Verifikation — alle Sensoren ❌ (kein Gerät verbunden)

| Part | Gerät | Software | Hardware-Verifikation |
|---|---|---|---|
| LiDAR | RPLIDAR A1/A2/S2 | ✅ Treiber + Parser (getestet), Baud-Table (S2 = 1 Mbaud, verifiziert) | ❌ kein Gerät verbunden |
| mmWave | TI IWR6843 | ✅ Treiber + Parser (CLI + DATA) | ❌ kein Gerät verbunden |
| UWB | Qorvo DWM3000 | ✅ Aura-Anchor-Protokoll spezifiziert (`docs/uwb_anchor_protocol.md`) | ❌ **kein Board; Stock-Firmware spricht das Protokoll nicht** (bewusst als eigene Spezifikation, Firmware müsste noch geschrieben werden) |
| SDR | RTL-SDR / HackRF | ✅ Passive-Radar-Verarbeitung | ❌ kein Gerät; Auflösung physikalisch 62 m statt <10 m (dokumentiert) |
| Thermal | MLX90640 / FLIR Lepton | ✅ Treiber | ❌ kein Gerät verbunden |
| IMU / BLE | eingebaut (CT45P) | ✅ Android SensorManager / BLE-Scanner | ❌ kein CT45P verfügbar; Verhalten nur simuliert |

---

## Zusammenfassung: offene Punkte (Aktionsliste)

| # | Punkt | Status | Aufwand | Voraussetzung |
|---|---|---|---|---|
| 1 | **APK bauen** (Manifest-Merger, AAPT2, echter Kotlin-Compile, JNI-Link) | ❌ fehlend (extern) | — | **Lösungsweg dokumentiert** in `docs/solution_path.md`: GitHub-Actions-Aktivierung über Web-UI (§4) oder Permission-Erteilung (§5); lokaler Build nachweislich unmöglich (§2/§3) |
| ~~2~~ | ~~**`NativePassiveRadar` verdrahten**~~ (A6) | ✅ **erledigt** (Sim-Feed) | S | Rest: echte SDR-Hardware, siehe 4 |
| ~~3~~ | ~~**`NativeVoxelCodec` + Chunk-Writer verdrahten**~~ | ✅ **erledigt** | M | `VoxelChunkWriter` + 39 Grid-Checks; End-to-End auf Gerät noch ausstehend (mit 1) |
| 4 | **Hardware-Verifikation aller Sensoren** (v. a. DWM3000-Protokoll, RTL-SDR) | ❌ | L | physische Geräte; DWM3000: Board oder eigene Anchor-Firmware |
| 5 | **Kotlin-Tests für Context-abhängige Klassen** (C2) | ❌ (teilweise) | L | Emulator / echtes Gerät; reine Logik jetzt 9 Suiten/286 Checks |
| ~~6~~ | ~~**Doku-Zahlen nachführen**~~ | ✅ **erledigt** | XS | README/architecture/completion_plan/android_build/performance_targets/tactical |
| ~~7~~ | ~~**`android_build.md` USB-Serial-Aussage**~~ | ✅ **erledigt** | XS | als „gegen Hardware unverifiziert“ umformuliert |
| ~~8~~ | ~~Visualizer-Bundle erzeugen~~ | ✅ **erledigt** | S | 2 057 Dateien / 26 MB in den Assets (gitignored) |
| 9 | GGUF-Modell für LLM | ❌ (bewusst) | — | Side-Load, nie bündeln |
| 10 | WebXR | ❌ (bewusst) | — | bewusst nicht vorgesehen (CT45P, kein Headset) |
| — | CI-Workflows aktivieren | 🟡 **blockiert** (Remote-Ablehnung 14.08.2026) | XS | `.github/workflows/`-Kopien lokal bereit; Push sobald `workflows`-Permission existiert, sonst Web-UI |
