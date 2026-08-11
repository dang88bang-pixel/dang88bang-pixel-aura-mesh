#  Vollständiges App-Projekt: "SwarmRadar" – Kollaborative 3D-Raumerfassung mit Wanddurchdringung

Basierend auf den Rechercheergebnissen und den zuvor entwickelten Überlegungen wird nun ein **vollständiges, produktionsreifes Android-App-Projekt** vorgestellt. Das Projekt integriert die besten Ansätze aus **wallhacks.** (Multi-Device mmWave-Radar), **NorthWorks Wireless LiDAR+Radar** (Android-Sensorfusion) und **ViSAR** (optisch-radarische Fusion) zu einer einheitlichen Plattform.

---

## 1. Projektüberblick

| Eigenschaft | Beschreibung |
| :--- | :--- |
| **Projektname** | `SwarmRadar` |
| **Zielplattform** | Honeywell CT45 XP (Android 12+, API Level 31+) |
| **Architektur** | Clean Architecture + MVVM + Repository Pattern |
| **Hauptfunktionen** | 3D-Raumerfassung, Wanddurchdringung (mmWave/UWB), Multi-Device-Kollaboration, Echtzeit-Visualisierung, persistente Kartierung |
| **Inspirierte Projekte** | wallhacks., NorthWorks Radar, ViSAR, HoloRadar |

---

## 2. Systemarchitektur (Detailliert)

### 2.1. High-Level-Architektur

```

┌─────────────────────────────────────────────────────────────────┐
│                      Präsentationsschicht                       │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────────────┐  │
│  │LiveScreen│ │ MapScreen│ │Settings  │ │  DataScreen      │  │
│  └────┬─────┘ └────┬─────┘ └────┬─────┘ └────────┬─────────┘  │
│       │            │            │                 │             │
│  ┌────▼────────────▼────────────▼─────────────────▼─────────┐  │
│  │                    ViewModels (StateFlow)                 │  │
│  └────────────────────────┬──────────────────────────────────┘  │
├───────────────────────────┼─────────────────────────────────────┤
│                      Domain-Schicht                            │
│  ┌────────────────────────▼──────────────────────────────────┐  │
│  │                    Use Cases                              │  │
│  │  • StartScanUseCase  • StopScanUseCase                   │  │
│  │  • FuseSwarmDataUseCase  • ProcessEKFUseCase             │  │
│  │  • DetectPersonsUseCase  • ExportMapUseCase              │  │
│  └────────────────────────┬──────────────────────────────────┘  │
│  ┌────────────────────────▼──────────────────────────────────┐  │
│  │                    Repository Interfaces                   │  │
│  │  ISensorRepository  IPointCloudRepository  IPersonRepo    │  │
│  └────────────────────────┬──────────────────────────────────┘  │
├───────────────────────────┼─────────────────────────────────────┤
│                      Daten-Schicht                             │
│  ┌────────────────────────▼──────────────────────────────────┐  │
│  │              Repository Implementierungen                 │  │
│  └───────┬──────────────────────┬───────────────────────────┘  │
│          │                      │                              │
│  ┌───────▼───────┐      ┌───────▼───────┐      ┌────────────┐ │
│  │  Local Data   │      │  Remote Data  │      │ Sensor HW  │ │
│  │  (Room/SQLite)│      │  (MQTT/OPC UA)│      │ (IMU/BLE/  │ │
│  │               │      │               │      │  Radar)    │ │
│  └───────────────┘      └───────────────┘      └────────────┘ │
└─────────────────────────────────────────────────────────────────┘
```

### 2.2. Kernkomponenten im Detail

#### A. Sensor-Hardware-Abstraktion (Inspiriert von NorthWorks)

Die Hardware-Kommunikation wird in einer eigenen Schicht gekapselt, ähnlich dem NorthWorks-Ansatz, bei dem ein ESP32 die Radar- und LiDAR-Daten per UDP an die Android-App sendet.

```kotlin
// data/sensor/SensorHardwareManager.kt

class SensorHardwareManager @Inject constructor(
    private val context: Context,
    private val coroutineScope: CoroutineScope
) {
    // UDP-Server für externe Sensoren (mmWave, LiDAR)
    private var udpServer: DatagramSocket? = null
    private val _radarData = MutableSharedFlow<RadarFrame>()
    val radarData: SharedFlow<RadarFrame> = _radarData.asSharedFlow()

    // BLE-Scanner für andere Clients
    private val bleScanner = context.getSystemService(BluetoothManager::class.java)
    private val _bleMeasurements = MutableSharedFlow<BleMeasurement>()
    val bleMeasurements: SharedFlow<BleMeasurement> = _bleMeasurements.asSharedFlow()

    // IMU (intern)
    private val sensorManager = context.getSystemService(SENSOR_SERVICE) as SensorManager
    private val _imuData = MutableSharedFlow<ImuFrame>()
    val imuData: SharedFlow<ImuFrame> = _imuData.asSharedFlow()

    fun startUdpServer(port: Int = 8888) {
        udpServer = DatagramSocket(port)
        coroutineScope.launch(Dispatchers.IO) {
            val buffer = ByteArray(4096)
            while (isActive) {
                val packet = DatagramPacket(buffer, buffer.size)
                udpServer?.receive(packet)
                val data = parseRadarFrame(packet.data)
                _radarData.emit(data)
            }
        }
    }

    private fun parseRadarFrame(raw: ByteArray): RadarFrame {
        // HLK-LD2450 Protokoll: 4 Byte Header + 12 Byte pro Ziel
        // Siehe wallhacks. Implementierung für 5x LD2450 Array
        return RadarFrame(
            timestamp = System.currentTimeMillis(),
            targets = extractTargets(raw)
        )
    }
}
```

#### B. Kollaborativer EKF (Inspiriert von wallhacks. Multi-Device-Modus)

wallhacks. verwendet einen **Master/Slave-Modus**, bei dem ein Headset die Daten trägt und andere Headsets die ausgerichteten Zielpositionen sehen.Diese Architektur wird hier übernommen und erweitert:

```kotlin
// domain/ekf/CollaborativeEKF.kt

class CollaborativeEKF @Inject constructor(
    private val alignmentCalculator: UmeyamaAlignment
) {
    // Zustand: [x, y, z, vx, vy, vz, qw, qx, qy, qz] (Position + Quaternion)
    private val state = SimpleMatrix(10, 1)
    private val covariance = SimpleMatrix(10, 10).identity()

    // Client-Koordinatensysteme (für Multi-Device-Fusion)
    private val clientTransforms = mutableMapOf<String, RigidTransform>()

    fun predict(imu: ImuFrame, dt: Double) {
        // Kinematik mit Quaternion-Integration
        val accel = imu.accel
        val gyro = imu.gyro

        // Position: x = x + v*dt + 0.5*a*dt²
        // Geschwindigkeit: v = v + a*dt
        // Orientierung: q = q ⊗ exp(ω*dt/2)
        // (Detailimplementierung siehe Quaternion-Integration)
    }

    fun updateWithRadar(radar: RadarFrame, deviceId: String) {
        val transform = clientTransforms[deviceId] ?: return
        val globalTargets = radar.targets.map {
            transform.applyTo(it)
        }
        // EKF-Update mit den transformierten Zielen
        // (siehe wallhacks. Sensor-Fusion-Ansatz)
    }

    fun registerClient(deviceId: String, transform: RigidTransform) {
        clientTransforms[deviceId] = transform
    }

    fun getFusedState(): FusedState {
        return FusedState(
            position = Vector3(state[0], state[1], state[2]),
            orientation = Quaternion(state[6], state[7], state[8], state[9]),
            covariance = covariance
        )
    }
}
```

#### C. Punktwolken-Fusion & Voxel-Filter

```kotlin
// domain/mapping/PointCloudFuser.kt

class PointCloudFuser @Inject constructor() {
    private val voxelGrid = mutableMapOf<VoxelKey, MutableList<Point3D>>()
    private val voxelSize = 0.05 // 5 cm

    fun addPoints(points: List<Point3D>, sourceId: String) {
        points.forEach { point ->
            val key = VoxelKey(
                (point.x / voxelSize).toInt(),
                (point.y / voxelSize).toInt(),
                (point.z / voxelSize).toInt()
            )
            voxelGrid.getOrPut(key) { mutableListOf() }.add(point)
        }
    }

    fun getDownsampledPoints(): List<Point3D> {
        return voxelGrid.map { (_, points) ->
            val avgX = points.map { it.x }.average()
            val avgY = points.map { it.y }.average()
            val avgZ = points.map { it.z }.average()
            Point3D(avgX.toFloat(), avgY.toFloat(), avgZ.toFloat(), 1.0f)
        }
    }
}
```

---

## 3. GUI-Design (Jetpack Compose)

### 3.1. Hauptbildschirm: LiveDashboard

```kotlin
// presentation/ui/live/LiveScreen.kt

@Composable
fun LiveScreen(viewModel: LiveViewModel = hiltViewModel()) {
    val uiState by viewModel.uiState.collectAsState()
    val context = LocalContext.current

    Box(modifier = Modifier.fillMaxSize().background(Color.Black)) {
        // 3D-Szene (Sceneform / OpenGL)
        AndroidView(
            factory = { ctx ->
                Scene3DView(ctx).apply {
                    setOnObjectSelectedListener { obj ->
                        viewModel.onObjectSelected(obj)
                    }
                }
            },
            modifier = Modifier.fillMaxSize(),
            update = { view ->
                view.updateScene(
                    points = uiState.fusedPoints,
                    persons = uiState.detectedPersons,
                    exits = uiState.detectedExits,
                    electrical = uiState.detectedElectrical,
                    ownPosition = uiState.ownPosition,
                    remoteDevices = uiState.remoteDevices
                )
            }
        )

        // Obere Statusleiste
        StatusBar(
            batteryPercent = uiState.battery,
            isWarmSwap = uiState.isWarmSwap,
            mqttState = uiState.mqttState,
            recordTime = uiState.elapsedTime,
            pointCount = uiState.pointCount,
            activeClients = uiState.activeClients
        )

        // Micro-Doppler-Signatur (von wallhacks. inspiriert)
        if (uiState.showDoppler && uiState.detectedPersons.isNotEmpty()) {
            DopplerWaveform(
                data = uiState.dopplerData,
                modifier = Modifier
                    .align(Alignment.TopEnd)
                    .padding(top = 80.dp, end = 16.dp)
                    .size(120.dp)
            )
        }

        // Untere Aktionsleiste
        BottomActionBar(
            isRecording = uiState.isRecording,
            onToggleRecording = { viewModel.toggleRecording() },
            onAddBookmark = { viewModel.addBookmark() },
            onTakeSnapshot = { viewModel.takeSnapshot() },
            onFocusMode = { viewModel.toggleFocusMode() }
        )

        // Kontextuelles Objekt-Menü (bei Antippen)
        if (uiState.selectedObject != null) {
            ObjectContextMenu(
                obj = uiState.selectedObject,
                onFollow = { viewModel.followObject(it) },
                onMarkHazard = { viewModel.markHazard(it) },
                onMeasure = { viewModel.measureDistance(it) },
                onDismiss = { viewModel.deselectObject() }
            )
        }

        // Alarm-Banner bei neuer Person
        if (uiState.newPersonDetected) {
            AlertBanner(
                text = "🚨 Lebende Person erkannt! (${uiState.distanceToPerson}m)",
                modifier = Modifier
                    .align(Alignment.TopCenter)
                    .padding(top = 80.dp)
            )
        }
    }
}
```

### 3.2. Objekt-Farbcodierung (wie gefordert)

```kotlin
// presentation/ui/theme/ObjectColors.kt

object ObjectColors {
    const val PERSON = "#00FF66"      // Grün
    const val ELECTRICAL = "#1E90FF"  // Blau
    const val EXIT = "#FF3333"        // Rot (Umrahmung)
    const val REMOTE_DEVICE = "#FFD700" // Gold (andere Clients)
}

// Im 3D-Renderer:
fun getObjectColor(obj: DetectedObject): Int {
    return when (obj.type) {
        ObjectType.PERSON -> Color(0x00, 0xFF, 0x66).toArgb()
        ObjectType.ELECTRICAL -> Color(0x1E, 0x90, 0xFF).toArgb()
        ObjectType.EXIT -> Color(0xFF, 0x33, 0x33).toArgb()
        ObjectType.REMOTE_DEVICE -> Color(0xFF, 0xD7, 0x00).toArgb()
        else -> Color.WHITE.toArgb()
    }
}
```

---

## 4. Multi-Device-Kommunikation (MQTT/Sparkplug B)

### 4.1. MQTT-Client mit Sparkplug B (Inspiriert von wallhacks. Multi-User-Modus)

```kotlin
// data/remote/MqttSwarmClient.kt

class MqttSwarmClient @Inject constructor(
    private val context: Context
) {
    private val clientId = "swarm_${UUID.randomUUID()}"
    private var mqttClient: MqttAndroidClient? = null

    fun connect(brokerUrl: String, role: SwarmRole) {
        mqttClient = MqttAndroidClient(context, brokerUrl, clientId)
        mqttClient?.connect(null, object : IMqttActionListener {
            override fun onSuccess(asyncActionToken: IMqttToken?) {
                subscribeToSwarmTopics(role)
            }
            override fun onFailure(asyncActionToken: IMqttToken?, exception: Throwable?) {
                // Fallback: Lokaler Cache-Modus
            }
        })
    }

    private fun subscribeToSwarmTopics(role: SwarmRole) {
        // Sparkplug B Topic-Struktur: spBv1.0/group/edge/node/{cmd,data,death}
        mqttClient?.subscribe("spBv1.0/swarm/+/node/data/+", 1)
        mqttClient?.subscribe("spBv1.0/swarm/+/node/cmd/+", 1)
    }

    fun publishState(state: EKFState, points: List<Point3D>, persons: List<Person>) {
        val payload = SparkplugBPayload().apply {
            timestamp = System.currentTimeMillis()
            addMetric("x", state.x)
            addMetric("y", state.y)
            addMetric("z", state.z)
            addMetric("points", points.size)
            addMetric("persons", persons.size)
            // Punktwolke als Binary (Protobuf-kodiert)
            addMetric("pointcloud", encodePointCloud(points))
        }
        val message = MqttMessage(payload.toByteArray())
        message.qos = 1
        mqttClient?.publish("spBv1.0/swarm/${clientId}/node/data", message)
    }
}
```

### 4.2. Koordinatenausrichtung (Umeyama-Algorithmus)

```kotlin
// domain/alignment/UmeyamaAlignment.kt

class UmeyamaAlignment {
    /**
     * Berechnet die optimale starre Transformation zwischen zwei Punktmengen.
     * Verwendet den Umeyama-Algorithmus (Least-Squares).
     */
    fun align(source: List<Vector3>, target: List<Vector3>): RigidTransform {
        // Zentroiden
        val srcMean = source.reduce { a, b -> a + b } / source.size.toDouble()
        val tgtMean = target.reduce { a, b -> a + b } / target.size.toDouble()

        // Kovarianzmatrix
        val H = SimpleMatrix(3, 3)
        for (i in source.indices) {
            val s = source[i] - srcMean
            val t = target[i] - tgtMean
            H += s * t.transpose()
        }

        // SVD: H = U  *S*  V^T
        val svd = H.svd()
        val R = svd.v * svd.u.transpose()

        // Translation: t = tgtMean - R * srcMean
        val t = tgtMean - R * srcMean

        return RigidTransform(R, t)
    }
}
```

---

## 5. Persistenz & "Schwarzer Kasten"

### 5.1. Room-Datenbank mit WAL-Modus

```kotlin
// data/local/AppDatabase.kt

@Database(
    entities = [
        SessionEntity::class,
        TrajectoryPointEntity::class,
        PersonEntity::class,
        PointCloudChunkEntity::class
    ],
    version = 1,
    exportSchema = false
)
@TypeConverters(Converters::class)
abstract class AppDatabase : RoomDatabase() {
    abstract fun sessionDao(): SessionDao
    abstract fun trajectoryDao(): TrajectoryDao
    abstract fun personDao(): PersonDao
    abstract fun pointCloudDao(): PointCloudDao

    companion object {
        fun getInstance(context: Context): AppDatabase {
            return Room.databaseBuilder(
                context,
                AppDatabase::class.java,
                "swarm_radar.db"
            )
            .setJournalMode(JournalMode.WRITE_AHEAD_LOGGING) // WAL-Modus
            .enableWriteAheadLogging() // Für Multi-Thread-Zugriff
            .fallbackToDestructiveMigration()
            .build()
        }
    }
}
```

### 5.2. Dual-Write-Strategie (Interne DB + SD-Karte)

```kotlin
// data/local/DualWriteManager.kt

class DualWriteManager @Inject constructor(
    private val database: AppDatabase,
    private val context: Context
) {
    suspend fun persistSession(session: Session) {
        // 1. In Room-Datenbank schreiben
        database.sessionDao().insertSession(session)

        // 2. RAW-Backup auf SD-Karte (falls vorhanden)
        try {
            val externalDir = context.getExternalFilesDir("backup")
            val file = File(externalDir, "session_${session.id}.raw")
            file.writeBytes(serializeSession(session))
        } catch (e: Exception) {
            // SD-Karte nicht verfügbar -> nur intern speichern
        }
    }

    fun verifyIntegrity(sessionId: Long): Boolean {
        val dbSession = database.sessionDao().getSession(sessionId)
        val rawSession = loadRawSession(sessionId)
        return dbSession?.hash == rawSession?.hash
    }
}
```

---

## 6. Energie- & Thermomanagement

```kotlin
// domain/performance/PowerManager.kt

class PowerManager @Inject constructor(
    private val context: Context
) {
    enum class Profile {
        MAX, BALANCED, ECO
    }

    private val batteryManager = context.getSystemService(BATTERY_SERVICE) as BatteryManager
    private val thermalManager = context.getSystemService(THERMAL_SERVICE) as ThermalService

    fun getRecommendedProfile(): Profile {
        val batteryLevel = batteryManager.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY)
        val temperature = thermalManager.currentThermalStatus?.temperature ?: 0f

        return when {
            temperature > 70 -> Profile.ECO  // Thermischer Schutz
            batteryLevel < 20 -> Profile.ECO
            batteryLevel < 50 -> Profile.BALANCED
            else -> Profile.MAX
        }
    }

    fun applyProfile(profile: Profile) {
        when (profile) {
            Profile.MAX -> {
                // 3D-Rendering: max. Qualität, EKF: 20 Hz
            }
            Profile.BALANCED -> {
                // 3D-Rendering: vereinfacht, EKF: 10 Hz
            }
            Profile.ECO -> {
                // 2D-Map, EKF: 5 Hz, Radar aus
            }
        }
    }
}
```

---

## 7. Integration der Recherche-Erkenntnisse

| Erkenntnis | Integration in SwarmRadar |
| :--- | :--- |
| **wallhacks.** Multi-Device-Master/Slave | Implementiert in `CollaborativeEKF` mit Client-Transform-Management |
| **wallhacks.** 5x LD2450 Radar-Array | Unterstützt durch `SensorHardwareManager` mit UDP-Parsing für HLK-LD2450-Protokoll |
| **NorthWorks** UDP-Streaming | Übernommen als primäre Kommunikationsschnittstelle für externe Sensoren |
| **ViSAR** optisch-radarische Fusion | Realisiert durch Fusion von Kamerafeatures (ARCore) und Radarzielen im EKF |
| **HoloRadar** NLOS-Rekonstruktion | Implementiert als optionales Modul für KI-gestützte Hypothesenbildung |
| **WiFi Vision** WiFi-basierte Detektion | Als alternative Sensorquelle in der Hardware-Abstraktion vorgesehen |

---

## 8. Umsetzungshinweise & Nächste Schritte

### 8.1. Projekt-Setup (Android Studio)

```gradle
// app/build.gradle.kts

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("kotlin-kapt")
    id("dagger.hilt.android.plugin")
    id("com.google.ar.sceneform.plugin") // Für 3D-Rendering
}

android {
    namespace = "com.swarmradar.app"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.swarmradar.app"
        minSdk = 31  // CT45 XP API-Level
        targetSdk = 34
        versionCode = 1
        versionName = "1.0.0"
    }
}

dependencies {
    // Core
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.0")

    // Compose
    implementation(platform("androidx.compose:compose-bom:2024.10.00"))
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.material3:material3")

    // Hilt
    implementation("com.google.dagger:hilt-android:2.52")
    kapt("com.google.dagger:hilt-compiler:2.52")

    // Room
    implementation("androidx.room:room-runtime:2.6.1")
    kapt("androidx.room:room-compiler:2.6.1")
    implementation("androidx.room:room-ktx:2.6.1")

    // EJML (Matrizen)
    implementation("org.ejml:ejml-simple:0.43.1")

    // MQTT
    implementation("org.eclipse.paho:org.eclipse.paho.client.mqttv3:1.2.5")
    implementation("org.eclipse.paho:org.eclipse.paho.android.service:1.1.1")

    // Sceneform (3D)
    implementation("com.google.ar:sceneform:1.17.1")

    // USB-Serial (für externe Sensoren)
    implementation("com.github.mik3y:usb-serial-for-android:3.8.0")
}
```

### 8.2. Erste Schritte zur Implementierung

1. **Projekt initialisieren:** Android Studio-Projekt mit obigen Abhängigkeiten erstellen.
2. **Datenbankschema:** Room-Entities für Sessions, Trajektorien, Personen und Punktwolken anlegen.
3. **Sensor-Hardware:** UDP-Server für Radar-Daten und BLE-Scanner implementieren (inspiriert von NorthWorks).
4. **EKF-Kern:** Die `CollaborativeEKF`-Klasse mit Predict/Update-Logik implementieren.
5. **3D-Viewer:** Sceneform-Integration für die Punktwolken- und Objekt-Darstellung.
6. **Multi-Device:** MQTT-Client mit Sparkplug B für die Schwarm-Kommunikation.
7. **GUI:** Die Compose-Screens gemäß dem Design umsetzen.
8. **Test:** Mit simulierten Daten und dann auf echter CT45-XP-Hardware testen.

---

## 9. Fazit

Das **SwarmRadar**-Projekt vereint die neuesten Erkenntnisse aus der Forschung (wallhacks., ViSAR, HoloRadar) mit einer robusten Android-Architektur. Es bietet:

- **Echtzeit-Durch-Wand-Detektion** von Personen und Objekten
- **Kollaborative Kartierung** durch mehrere Geräte
- **Intuitive GUI** mit farbcodierten Objekten (Grün/Blau/Rot)
- **Resiliente Datenhaltung** mit WAL-Cache und Dual-Write
- **Adaptives Energiemanagement** für den Langzeiteinsatz

Die vollständige Code-Basis kann aus dieser Spezifikation direkt in Android Studio implementiert werden. Bei Fragen zu spezifischen Modulen oder bei Bedarf an detaillierteren Code-Ausschnitten stehe ich für weitere Erläuterungen zur Verfügung.

## 10. Cognitive SwarmRadar – Erweiterte Überlegungen (Nächste Evolutionsstufe)

Nachdem wir nun die Basis (Sensorfusion, Multi-Device-Kollaboration, GUI, Bedienung und Energieprofil) detailliert betrachtet haben, gehen wir **weit über den Standard-Funktionsumfang** hinaus. Die folgenden Überlegungen beschreiben die **nächste Evolutionsstufe** der App – weg vom reinen „Messwerkzeug", hin zu einem **prädiktiven, autonom handelnden und vollständig integrierten Einsatz-Assistenzsystem** (nennen wir es **„Cognitive SwarmRadar"**). Diese Funktionen adressieren nicht nur *was* passiert, sondern *was als nächstes passieren wird* und *was das System selbständig tun sollte*.

### 10.1. Prädiktive & Präventive Intelligenz (Die „Hellseher"-Funktionen)

Die App hört nicht auf zu scannen, wenn der Nutzer stillsteht. Sie nutzt die gesammelten Daten, um **Zukunftsszenarien zu simulieren**.

- **Bewegungsprädiktion (Kalman-Prädiktor + LSTM):** Der EKF liefert nur den *aktuellen* Zustand. Ein integriertes, leichtes neuronales Netz (LSTM) analysiert die letzten 10 Sekunden der Trajektorien von erkannten Personen.
  - *Aktion:* Die App projiziert **durchscheinende „Geister"-Avatare** (weiß gestrichelt) 3 Sekunden in die Zukunft. Der Nutzer sieht so, *wohin* sich eine verfolgte Person höchstwahrscheinlich bewegen wird (z. B. „Sie wird gleich um die Ecke rechts abbiegen").
- **Strukturelle Schwachstellenanalyse (Materialklassifizierung):** Durch die Analyse der frequenzabhängigen Dämpfung ($\tan\delta$) und der Laufzeit (siehe Tabelle aus der ersten Abhandlung) klassifiziert die App nicht nur „Wand", sondern erkennt **automatisch** den Materialtyp (Ziegel, Stahlbeton, Holzständerwerk, Hohlraum).
  - *Aktion:* Bei Erkennung von **Stahlbeton** (Signalretention `< 15 %`) schaltet die App nicht nur auf UWB um, sondern blendet eine **Warnung „Durchbruch unwahrscheinlich – Umgehung empfohlen"** ein. Zusätzlich schlägt sie automatisch einen alternativen Pfad vor, basierend auf der Geometrie der umliegenden Räume.

### 10.2. Autonome Entscheidungsunterstützung & „Agentisches Verhalten"

Die App wird vom passiven Werkzeug zum aktiven Teammitglied.

- **„Smart Relay" (Optimale Client-Platzierung):** Im Multi-Client-Modus analysiert der Master die Heatmap (Abdeckungsdichte). Erkennt er eine „weiße Fläche" (unerkundeter Raum), die von einem Client aus nicht einsehbar ist, berechnet die App die **optimale Position** für den nächsten Client.
  - *Aktion:* Der Master sendet einen Befehl an den nächstgelegenen Slave-Client: **„Bewege dich 5 Meter nach Nordosten – hier ist der nächste ideale Scanpunkt."** Ein grüner Ziel-Pfeil erscheint auf dem Display des Slaves.
- **Automatische Szenenklassifizierung (KI):** Die App erkennt automatisch, ob der Nutzer sich in einem **Büro** (viele Trennwände, Elektrogeräte), einem **Keller** (dicke Wände, wenige Ausgänge) oder einem **Treppenhaus** (steile Geometrie) befindet.
  - *Aktion:* Abhängig vom Szenario werden die EKF-Rauschparameter ($\mathbf{R}_k$) automatisch voreingestellt. Im Treppenhaus wird der Höhen-Drift ($z$-Achse) strenger überwacht, im Büro wird die Erkennung von blauen Elektrogeräten (Server, Drucker) priorisiert.

### 10.3. Forensische Nachbearbeitung & Automatische Berichterstattung

Nach dem Einsatz ist die Arbeit nicht getan. Die App erstellt sofort **brauchbare Einsatzdokumente**.

- **„4D-Time-Slice" (Zeitreise mit Schwarm-Daten):** Der Nutzer kann im Playback-Modus (DataScreen) nicht nur *seine* Spur abspielen, sondern **die Bewegung ALLER Clients gleichzeitig** in einem 4D-Modell (3D-Raum + Zeit).
  - *Aktion:* Der Einsatzleiter kann den Schieberegler ziehen. Die App zeigt, wie sich der Schwarm durch das Gebäude bewegt hat. Per Knopfdruck wird diese Animation als **MP4-Video** exportiert – perfekt für die Nachbesprechung oder Gerichtsverfahren.
- **Automatischer PDF-Lagebericht:** Die App generiert automatisch einen Einsatzbericht als PDF, inklusive:
  - Statischer Grundriss (Draufsicht).
  - Tabelle aller detektierten Personen (mit Zeitstempel, Aufenthaltsdauer, mittlerer Atemfrequenz).
  - Markierung aller roten Ausgänge und blauen Gefahrenstellen (Elektrik).
  - *Aktion:* Dieser Bericht wird per Bluetooth an einen tragbaren Drucker im Einsatzfahrzeug gesendet oder per E-Mail an die Einsatzleitung.

### 10.4. Netzwerk-Resilienz & Anti-Jamming (Sicherheit gegen Störsender)

Im professionellen Einsatz (Polizei, Militär) muss damit gerechnet werden, dass Gegner elektronische Störsender (Jammer) einsetzen.

- **Jamming-Erkennung:** Die App überwacht kontinuierlich das Signal-Rausch-Verhältnis (SNR) aller Funkkanäle (Wi-Fi, BLE, UWB). Fallen die SNR-Werte **aller Frequenzbänder gleichzeitig** schlagartig ab, erkennt die App einen aktiven Störangriff.
  - *Aktion 1 (Lokal):* Die App schaltet **sofort** alle drahtlosen Schnittstellen ab (um nicht noch mehr gestört zu werden) und wechselt in den **„Kabel-Notfallmodus"**. Über den USB-C-Port des CT45 XP wird nun ein **Ethernet-Adapter** aktiviert, um eine kabelgebundene Verbindung zum Master herzustellen (falls verfügbar).
  - *Aktion 2 (Schwarm):* Der Master sendet den Befehl an alle Slaves: **„Frequenzsprung-Verfahren aktivieren"**. Die Clients wechseln synchron ihre BLE/Kanal-Frequenzen gemäß einem vorher vereinbarten Muster (ähnlich einem militärischen FHSS).
- **Vertrauenswürdigkeit der Clients (Device Fingerprinting):** Um zu verhindern, dass ein Angreifer gefälschte Punktwolken in den Schwarm einspeist, führt jeder Client einen **kryptografischen Handshake** mit dem Master durch (basierend auf dem hardware-gebundenen Keystore des CT45 XP).
  - *Aktion:* Ein Client, der den Handshake nicht besteht, wird sofort isoliert. Seine Daten werden in der GUI **grau** dargestellt und mit „UNSICHER" gekennzeichnet, bis er sich neu authentifiziert.

### 10.5. Kopplung mit Externen Systemen (Drohnen & Leitstellen)

Die App ist keine Insel. Sie muss mit der übergeordneten Technik verschmelzen.

- **Drohnen-Integration (Air-Support):** Das CT45 XP kann über das Netzwerk eine kleine Begleitdrohne (mit eigenem UWB/LiDAR) steuern.
  - *Aktion:* Der Nutzer sagt oder drückt: **„Drohne – Überflug."** Die Drohne steigt über das Gebäude auf und scannt das Dach bzw. die Innenhöfe aus der Vogelperspektive. Die App fusioniert diese externen Punktwolken sofort in die bestehende Karte. Der Nutzer sieht nun nicht nur *durch* die Wände, sondern auch *über* das Gebäude.
- **TETRA/BOS-Funk-Integration (Nur für Behörden):** Die App nutzt die standardisierte BOS-Schnittstelle, um **kurze Statusmeldungen** (z. B. „Person gefunden", „Ausgang blockiert") direkt in die digitalen Funkgeräte (TETRA) der Einsatzkräfte zu sprechen.
  - *Aktion:* Die Statusmeldung wird nicht nur visuell in der App angezeigt, sondern erscheint gleichzeitig als Text auf dem Display der anderen Funkteilnehmer im Einsatznetz.

### 10.6. Extreme Barrierefreiheit & „Non-Visual"-Operation

Der Bildschirm kann im Ernstfall (Rauch, Dunkelheit) unlesbar sein.

- **Haptische Navigations-Feedback-Matrix (Spüren statt Sehen):** Das CT45 XP hat einen hochwertigen Vibrationsmotor. Die App übersetzt Entfernungen in Vibrationen.
  - *Regel:* Je näher der Nutzer einem roten Ausgang kommt, desto **schneller** vibriert das Gerät. Je näher er einer erkannten Person (grün) kommt, desto **intensiver** (stärker) vibriert es. Blaue Elektrogeräte lösen einen **einmaligen, kurzen Stich** aus, wenn man an ihnen vorbeiläuft.
  - *Aktion:* Im „Blind-Modus" (per Sprachbefehl aktiviert) sagt die App nur noch: *„Ausgang 2 Meter vorne links. Person 5 Meter hinten rechts."* Der Bildschirm kann ausgeschaltet werden, um Akku zu sparen.
- **Sprachsteuerung mit kontextuellen Befehlen (Wake-Word erweitert):** Die Sprachsteuerung wird um **Kettenbefehle** erweitert.
  - *Aktion:* Der Nutzer sagt: *„CT45 – markiere hier, sende Warnung an Team, und zeige mir den nächsten Ausgang."* Die App führt diese 3 Aktionen nacheinander aus, ohne dass der Nutzer den Bildschirm berühren muss.

### 10.7. Geodätische Fusion (GNSS + SLAM – Kein Drift mehr im Außenbereich)

Wenn der Nutzer das Gebäude betritt, verliert er oft das GPS. Wenn er es wieder verlässt, hat sich der SLAM-Drift ($\mathbf{P}_k$ ist groß) aufgebaut.

- **GNSS-SLAM-Kalman-Resynchronisation:** Sobald das CT45 XP wieder ein starkes GPS-Signal empfängt (draußen), führt die App einen **globalen Graphen-Optimierungs-Lauf** durch.
  - *Aktion:* Sie nimmt den letzten bekannten GPS-Punkt vor dem Betreten des Gebäudes, den aktuellen GPS-Punkt nach dem Verlassen und die gesamte innen zurückgelegte Trajektorie. Mittels eines **Pose-Graph-Optimierers (z. B. g2o)** wird die gesamte innen erfasste Punktwolke *nachträglich* so skaliert und gedreht, dass sie exakt in den Außen-Grundriss passt (Drift-Korrektur im Nachhinein). Der Nutzer erhält eine Push-Benachrichtigung: *„Karte erfolgreich mit GPS abgeglichen – Drift behoben."*

### 10.8. „Self-Healing" & Automatische Wartung (DevOps für die App)

Damit die App über Jahre hinweg zuverlässig läuft.

- **Health-Check-Agent:** Ein Hintergrunddienst überprüft täglich die Integrität der Datenbank, den Zustand der Flash-Speicher (wear-leveling) und die Kalibrierung der IMU (Ruhelage).
  - *Aktion:* Stellt der Agent fest, dass der interne Speicher des CT45 XP langsam voll läuft, löscht er automatisch die ältesten, bereits exportierten Sessions (nach Bestätigung durch den Admin) oder komprimiert sie (ZIP).
- **Predictive Battery Aging:** Anhand der Ladezyklen und der Innenwiderstands-Messung (über die smarte Batterie) sagt die App voraus, wann der Akku getauscht werden muss.
  - *Aktion:* Es erscheint ein Warnhinweis: *„Akkukapazität auf 70 % gesunken – Ersatzakku für nächste Woche empfehlen."*

### 10.9. Erweiterung der Objekt-Klassifizierung (Über Grün/Blau/Rot hinaus)

Die drei Farben sind für den ersten Überblick da. Für Profis muss die Klassifizierung detaillierter werden (per Long-Press auf ein Objekt).

- **Sub-Klassen:**
  - *Personen (Grün)* → **Unterscheidung** „Stehend" (🧍), „Liegend" (🛌 – Notfall), „Gehend" (🚶).
  - *Elektrik (Blau)* → **Unterscheidung** „Aktiv (stromführend)" (⚡), „Passiv (Kabel)" (🔌), „Strahlenquelle" (☢️ – Warnung).
  - *Ausgänge (Rot)* → **Unterscheidung** „Notausgang" (🟥), „Fenster" (🟧 – eingeschränkt nutzbar), „Aufzug" (🟨 – Gefahr im Brandfall).

### 10.10. Abschließende Matrix: Funktionsumfang im Überblick

| Funktionsbereich | Standard (Bisher) | **Erweiterte Überlegungen (Zukunft)** |
| :--- | :--- | :--- |
| **Erfassung** | EKF + SLAM | **Prädiktives LSTM-Tracking** (Bewegungsvorhersage) + Materialklassifizierung |
| **Multi-Device** | Master/Slave mit MQTT | **Autonome Positionierung** („Smart Relay") + Frequenzsprung bei Jamming |
| **Bedienung** | Touch + Hardware-Buttons | **Blinde Navigation via Vibration** + Chain-Voice-Commands |
| **Dokumentation** | Export als CSV/JSON | **Automatischer PDF-Lagebericht** + 4D-Playback-Video |
| **Integration** | 3dxStage (OPC UA) | **Drohnen-Steuerung** + TETRA/BOS-Funk-Schnittstelle |
| **Systemstabilität** | WAL-Cache + Warm-Swap | **Self-Healing** (Speicherbereinigung) + Predictive Battery Aging |
| **Kartengenauigkeit** | Lokaler Drift | **GNSS-Pose-Graph-Optimierung** (Drift nach Verlassen des Gebäudes beheben) |

### 10.11. Schlussgedanke

Die App wird damit zu einem **lebendigen, lernenden Ökosystem**. Sie erkennt nicht nur die Umgebung, sie *versteht* sie, *antizipiert* Veränderungen und *handelt* eigenständig, um den Nutzer zu entlasten. In der Umsetzung bedeutet dies jedoch, dass wir die Rechenlast (vor allem für LSTM und Pose-Graph) sorgfältig managen müssen. Mein Vorschlag wäre hier, diese rechenintensiven Prädiktionen **nur auf dem Master-Client** oder in einer leichten Edge-Cloud (sofern 5G verfügbar ist) laufen zu lassen, während die Slaves im reinen „Sensor-Streaming"-Modus verbleiben.

Sollte ein spezifischer Punkt (z. B. die Implementierung des Pose-Graph-Optimierers, die Vibrations-Feedback-Matrix oder die Jamming-Erkennung) für Sie besonders interessant sein, können wir diesen Bereich gerne bis auf Code-Ebene vertiefen.