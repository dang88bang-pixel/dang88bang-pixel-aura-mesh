## Vollständige App-Projektvorlage für mobile 3D-Raumerfassung mit Wanddurchdringung (Honeywell CT45 XP / Android)

Diese Anleitung liefert ein **sofort umsetzbares** Android-App-Projekt, das die beschriebene Systemarchitektur auf dem Honeywell CT45 XP (oder jedem Android-Gerät mit kompatibler Sensorik) abbildet. Die App ist modular aufgebaut, enthält eine grafische Benutzeroberfläche (GUI) mit Echtzeit-3D-Visualisierung, Sensorfusion (EKF), BLE-Triangulation, optionaler Radar-Anbindung und persistenter Datenhaltung.

> **Hinweis**: Das CT45 XP bietet herstellerspezifische APIs (z. B. `com.honeywell.df.beacontransmitter`, Warm‑Swap‑Intents). Diese werden im Code abstrahiert und sind durch Platzhalter gekennzeichnet. Für die reine Simulation oder Entwicklung auf Standard‑Android kann auf die generischen Android‑Sensor‑APIs zurückgegriffen werden.

## 1. Projektüberblick

**Ziel**: Ein mobiles Edge‑System, das

- inertiale Sensoren (IMU), BLE‑Signalstärke, GNSS und optionale externe Radarmodule fusioniert,
- einen adaptiven Extended Kalman Filter (EKF) zur Positions‑ und Geschwindigkeitsschätzung betreibt,
- detektierte Personen hinter Wänden (via Micro‑Doppler) als Avatare anzeigt,
- die erfassten 3D‑Punktwolken und Trajektorien in einer interaktiven 3D‑Ansicht visualisiert,
- die Daten persistent (SQLite) speichert und über MQTT/OPC UA an übergeordnete Systeme (z. B. 3dxStage) überträgt.

**Technologie‑Stack**:

- **Sprache**: Kotlin (Android native)
- **UI**: Jetpack Compose (moderne deklarative UI) oder XML‑basierte Views (hier Compose gewählt)
- **Architektur**: Clean Architecture mit Use Cases, Repository‑Pattern, Dependency Injection (Hilt)
- **Sensorzugriff**: Android SensorManager, BluetoothLeScanner, LocationManager
- **EKF**: Eigen3‑Port für Android (oder einfache Matrizen‑Bibliothek wie `EJML`)
- **Datenbank**: Room (SQLite) mit WAL‑Modus
- **MQTT**: Eclipse Paho Android Service
- **3D‑Visualisierung**: Sceneform (Google ARCore‑basierte 3D‑Engine) oder OpenGL ES – hier **Sceneform** für einfache Handhabung.
- **Netzwerk**: Retrofit für REST, OPC UA über Milo‑Client (optional)

## 2. GUI‑Design (Compose‑Screens)

Die App besteht aus **vier Hauptbildschirmen**, die über eine Bottom‑Navigation erreichbar sind:

### Screen 1: Live‑Dashboard (**`LiveScreen`

- **Kopfzeile**: Status (Erfassung läuft / gestoppt), Akkustand, Verbindungsqualität (Wi‑Fi, MQTT)
- **3D‑Canvas**: Interaktive 3D‑Szene mit:
  - Punktwolke (farbkodiert nach Höhe oder Intensität)
  - Eigenposition (roter Würfel) mit Richtungsvektor
  - Detektierte Personen (grüne Kugeln) mit Konfidenzring
  - Raumkonturen (Linien)
- **Überlagerte Echtzeit‑Werte**:
  - Aktuelle Position (x, y, z) in Metern
  - Geschwindigkeit (vx, vy, vz)
  - Anzahl erfasster Punkte
  - Anzahl lebender Personen
- **Steuerknöpfe**: Start/Stopp, Reset, Momentaufnahme (Screenshot)

### Screen 2: Kartierung & Objekte (**`MapScreen`

- **2D‑Draufsicht** (vereinfachte Grundrissansicht) der erfassten Umgebung
- Filterbare Liste der detektierten Objekte (Zeitstempel, Typ, Position, Konfidenz)
- Möglichkeit, Objekte zu markieren und Details anzuzeigen

### Screen 3: Einstellungen (**`SettingsScreen`

- Sensor‑Konfiguration:
  - Aktivieren/Deaktivieren von IMU, BLE, GNSS, Kamera (SLAM), externem Radar
  - Abtastraten (IMU 100 Hz, Radar 20 Hz)
  - Rauschvarianzen (σ²) für EKF (manuell oder automatisch)
- Netzwerk:
  - MQTT‑Broker‑URL, Topic‑Struktur (Sparkplug B)
  - OPC UA‑Endpoint (optional)
- Datenhaltung:
  - Löschintervall für Cache, Export‑Format (CSV, JSON)
- Kalibrierung:
  - Magnetometer‑Kalibrierung, BLE‑Referenzpunkte setzen

### Screen 4: Daten & Export (**`DataScreen`

- Liste der gespeicherten Sitzungen (mit Datum, Dauer, Punktzahl)
- Vorschau der gespeicherten Trajektorie
- Export‑Button (teilt Datei über Android‑Share)

## 3. Software‑Architektur (Clean Architecture)

```

app/
├── data/
│   ├── local/                # Room‑Datenbank, DAOs, Entities
│   ├── remote/               # MQTT/OPC UA‑Client, Retrofit‑Service
│   └── repository/           # Implementierungen der Repositories
├── domain/
│   ├── model/                # Datenklassen (Position, PointCloud, Person)
│   ├── repository/           # Schnittstellen (z.B. ISensorRepository)
│   └── usecase/              # Anwendungslogik (StartScan, StopScan, ProcessEKF)
├── presentation/
│   ├── ui/                   # Compose‑Screens, ViewModels
│   ├── navigation/           # NavHost, Destinations
│   └── theme/                # Farben, Typografie
└── di/                       # Hilt‑Module für Abhängigkeiten

```
## 4. Kernmodule im Detail

### 4.1. SensorService (Foreground Service)

Ein `SensorService` läuft als Vordergrund‑Service, um auch bei geschlossener App Sensordaten zu erfassen. Er implementiert:

- `SensorEventListener` für IMU (Beschleunigung, Gyro, Magnetfeld)
- `ScanCallback` für BLE (RSSI, optional AoA)
- `LocationListener` für GNSS (optional, im Außenbereich)
- Externe Radar‑Daten über USB‑Seriell oder UDP (hier abstrahiert über `RadarDataReceiver`)

**Code‑Ausschnitt** (Kotlin):

```kotlin

class SensorService : Service(), SensorEventListener, ScanCallback {
    private lateinit var sensorManager: SensorManager
    private val ekfProcessor = EKFProcessor() // EKF-Instanz
    private val pointCloudBuilder = PointCloudBuilder()
    private val mqttClient = MqttClient(...)

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        startForeground(NOTIFICATION_ID, createNotification())
        sensorManager = getSystemService(SENSOR_SERVICE) as SensorManager
        registerSensors()
        startBleScan()
        return START_STICKY
    }

    override fun onSensorChanged(event: SensorEvent) {
        val timestamp = event.timestamp
        when (event.sensor.type) {
            Sensor.TYPE_ACCELEROMETER -> {
                val accel = event.values // [ax, ay, az]
                ekfProcessor.predict(accel, timestamp)
            }
            Sensor.TYPE_GYROSCOPE -> {
                val gyro = event.values // [ωx, ωy, ωz]
                ekfProcessor.updateOrientation(gyro, timestamp)
            }
        }
        // Nach jedem Sensorereignis: optional EKF-Update mit anderen Messungen
        ekfProcessor.updateWithRadar(getLatestRadarData())
        publishState()
    }

    override fun onScanResult(callbackType: Int, result: ScanResult) {
        val rssi = result.rssi
        val address = result.device.address
        // BLE-Triangulation: Abstand schätzen und als Messung einfließen lassen
        val distance = estimateDistance(rssi) // Freiraummodell mit Korrektur
        ekfProcessor.updateWithBle(distance, result.timestampNanos)
    }
}

```
### 4.2. Extended Kalman Filter (EKF) – Implementierung

Der EKF wird als eigenständige Klasse `EKFProcessor` realisiert. Zustandsvektor: $\mathbf{x} = [x, y, z, v_x, v_y, v_z]^T$.

Für die Matrizenoperationen wird die Bibliothek **EJML** (Efficient Java Matrix Library) verwendet (alternativ Kotlin‑Mathe‑Bibliotheken).

```kotlin

import org.ejml.simple.SimpleMatrix

class EKFProcessor {
    private val dt = 0.05 // 50 ms
    private val F = SimpleMatrix(6, 6).apply {
        set(0,0,1.0); set(0,3,dt)
        set(1,1,1.0); set(1,4,dt)
        set(2,2,1.0); set(2,5,dt)
        set(3,3,1.0); set(4,4,1.0); set(5,5,1.0)
    }
    private val Q = SimpleMatrix(6, 6).apply {
        // Prozessrauschen (angepasst an Bewegungsdynamik)
    }
    private val x = SimpleMatrix(6, 1) // Zustand
    private val P = SimpleMatrix(6, 6).identity() // Kovarianz

    fun predict(accel: FloatArray, timestamp: Long) {
        // Kinematik: v = v + a*dt (vereinfacht)
        val aX = accel[0].toDouble()
        val aY = accel[1].toDouble()
        val aZ = accel[2].toDouble()
        val control = SimpleMatrix(6,1).apply {
            set(3,0, aX*dt)
            set(4,0, aY*dt)
            set(5,0, aZ*dt)
        }
        x = F.mult(x).plus(control)
        P = F.mult(P).mult(F.transpose()).plus(Q)
    }

    fun updateWithBle(distance: Double, timestamp: Long) {
        // Messmodell: h(x) = sqrt((x - x_ble)^2 + ...)
        // Jacobi-Matrix H wird berechnet
        val z = SimpleMatrix(1,1).apply { set(0,0, distance) }
        val H = SimpleMatrix(1,6) // partielle Ableitung nach x,y,z
        val R = SimpleMatrix(1,1).apply { set(0,0, sigmaBle*sigmaBle) }
        kalmanUpdate(z, H, R)
    }

    fun updateWithRadar(radarData: RadarData) {
        // Radar liefert direkt Position (x,y,z) und Geschwindigkeit?
        // Oder nur Entfernung und Winkel?
        // Hier wird beispielhaft eine Positionsmessung angenommen.
        val z = SimpleMatrix(3,1).apply {
            set(0,0, radarData.x)
            set(1,0, radarData.y)
            set(2,0, radarData.z)
        }
        val H = SimpleMatrix(3,6).apply {
            set(0,0,1.0); set(1,1,1.0); set(2,2,1.0)
        }
        val R = SimpleMatrix(3,3).apply {
            set(0,0, sigmaRadar*sigmaRadar)
            // ...
        }
        kalmanUpdate(z, H, R)
    }

    private fun kalmanUpdate(z: SimpleMatrix, H: SimpleMatrix, R: SimpleMatrix) {
        val y = z.minus(H.mult(x))
        val S = H.mult(P).mult(H.transpose()).plus(R)
        val K = P.mult(H.transpose()).mult(S.invert())
        x = x.plus(K.mult(y))
        val I = SimpleMatrix.identity(6)
        P = I.minus(K.mult(H)).mult(P)
    }

    fun getState(): SimpleMatrix = x
    fun getCovariance(): SimpleMatrix = P
}

```
### 4.3. Punktwolken‑ und Objekterkennung

Die Punktwolke wird in einer `PointCloudBuilder`‑Klasse aufgebaut, die Punkte aus Kamera‑SLAM (via ARCore) und Radar‑Reflexionen zusammenführt. Für jede detektierte Person (Micro‑Doppler) wird ein `Person`‑Objekt mit Position, Konfidenz und Atemfrequenz erstellt.

```kotlin

data class Point3D(val x: Float, val y: Float, val z: Float, val intensity: Float, val source: String)
data class Person(val id: String, val x: Float, val y: Float, val z: Float, val confidence: Float, val breathingRate: Float)

class PointCloudBuilder {
    private val points = mutableListOf<Point3D>()
    private val persons = mutableListOf<Person>()

    fun addPoint(point: Point3D) { points.add(point) }
    fun addPerson(person: Person) { persons.add(person) }
    fun getPoints(): List<Point3D> = points
    fun getPersons(): List<Person> = persons
}

```
### 4.4. Persistenz (Room)

Die Datenbank speichert:

- `Session` (Startzeit, Endzeit, Anzahl Punkte)
- `TrajectoryPoint` (Zeitstempel, x, y, z, vx, vy, vz)
- `DetectedPerson` (Zeitstempel, ID, x, y, z, Konfidenz, Atemfrequenz)

**DAO**:

```kotlin

@Dao
interface TrajectoryDao {
    @Insert
    suspend fun insertPoint(point: TrajectoryPoint)
    @Query("SELECT * FROM trajectory WHERE sessionId = :sessionId ORDER BY timestamp")
    fun getPointsForSession(sessionId: Long): Flow<List<TrajectoryPoint>>
}

```
### 4.5. MQTT/OPC UA Kommunikation

Ein `MqttClient`‑Wrapper sendet periodisch den aktuellen Zustand (Position, Punkte, Personen) im Sparkplug B‑Format (Protobuf). Dazu wird die Bibliothek `org.eclipse.paho.android.service` verwendet.

```kotlin

class MqttClient(private val brokerUrl: String, private val clientId: String) {
    private val client = MqttAndroidClient(context, brokerUrl, clientId)

    fun publishState(state: EKFState, persons: List<Person>) {
        val payload = SparkplugBPayload.Builder()
            .setTimestamp(System.currentTimeMillis())
            .addMetric("x", state.x)
            .addMetric("y", state.y)
            // ...
            .addMetric("persons", persons.size)
            .build()
        val message = MqttMessage(payload.toByteArray())
        message.qos = 1
        client.publish("sparkplug/group/edge/node/data", message)
    }
}

Für OPC UA kann der **Milo‑Client** (Eclipse) integriert werden – hier beschränken wir uns auf MQTT als Standard.

```
### 4.6. 3D‑Visualisierung mit Sceneform

Sceneform ermöglicht die einfache Darstellung von 3D‑Inhalten über ARCore oder als reine Render‑Engine. In der `LiveScreen` wird eine `Sceneform`‑View eingebettet.

```kotlin

class Scene3DView(context: Context) : View(context) {
    private lateinit var scene: Scene
    private val renderer = SceneformRenderer()

    fun update(pointCloud: List<Point3D>, persons: List<Person>, position: Vector3) {
        // Punkte als kleine Kugeln (oder Punkte via OpenGL) hinzufügen
        // Personen als grüne Kugeln mit Label
        // Kamera auf Position des Nutzers setzen
    }
}

```
**Alternativ**: Bei einfacheren Anforderungen kann eine `SurfaceView` mit OpenGL ES oder die Bibliothek **Rajawali** verwendet werden.

## 5. Projektstruktur (Package‑Übersicht)

```

com.example.radarapp/
├── domain/
│   ├── model/
│   │   ├── Point3D.kt
│   │   ├── Person.kt
│   │   ├── SensorData.kt
│   │   └── EKFState.kt
│   ├── repository/
│   │   ├── ISensorRepository.kt
│   │   ├── IPointCloudRepository.kt
│   │   └── IPersonRepository.kt
│   └── usecase/
│       ├── StartScanUseCase.kt
│       ├── StopScanUseCase.kt
│       └── ProcessEKFUseCase.kt
├── data/
│   ├── local/
│   │   ├── AppDatabase.kt
│   │   ├── dao/
│   │   │   ├── TrajectoryDao.kt
│   │   │   └── PersonDao.kt
│   │   └── entities/
│   │       ├── TrajectoryPointEntity.kt
│   │       └── PersonEntity.kt
│   ├── remote/
│   │   ├── MqttClient.kt
│   │   └── SparkplugBEncoder.kt
│   └── repository/
│       ├── SensorRepositoryImpl.kt
│       ├── PointCloudRepositoryImpl.kt
│       └── PersonRepositoryImpl.kt
├── presentation/
│   ├── ui/
│   │   ├── live/
│   │   │   ├── LiveScreen.kt
│   │   │   └── LiveViewModel.kt
│   │   ├── map/
│   │   │   ├── MapScreen.kt
│   │   │   └── MapViewModel.kt
│   │   ├── settings/
│   │   │   ├── SettingsScreen.kt
│   │   │   └── SettingsViewModel.kt
│   │   └── data/
│   │       ├── DataScreen.kt
│   │       └── DataViewModel.kt
│   ├── navigation/
│   │   └── AppNavHost.kt
│   └── theme/
│       └── Theme.kt
├── di/
│   ├── AppModule.kt
│   ├── DatabaseModule.kt
│   ├── MqttModule.kt
│   └── SensorModule.kt
└── service/
    ├── SensorService.kt
    ├── RadarDataReceiver.kt
    └── NotificationHelper.kt

```
## 6. Abhängigkeiten (build.gradle)

```gradle

dependencies {
    // Jetpack Compose
    implementation platform('androidx.compose:compose-bom:2024.10.00')
    implementation 'androidx.compose.ui:ui'
    implementation 'androidx.compose.material3:material3'
    implementation 'androidx.compose.ui:ui-tooling-preview'
    implementation 'androidx.activity:activity-compose:1.9.0'
    implementation 'androidx.navigation:navigation-compose:2.7.7'

    // Room
    implementation 'androidx.room:room-runtime:2.6.1'
    kapt 'androidx.room:room-compiler:2.6.1'
    implementation 'androidx.room:room-ktx:2.6.1'

    // Hilt
    implementation 'com.google.dagger:hilt-android:2.52'
    kapt 'com.google.dagger:hilt-compiler:2.52'
    implementation 'androidx.hilt:hilt-navigation-compose:1.2.0'

    // EJML für Matrizen
    implementation 'org.ejml:ejml-simple:0.43.1'

    // MQTT (Paho)
    implementation 'org.eclipse.paho:org.eclipse.paho.client.mqttv3:1.2.5'
    implementation 'org.eclipse.paho:org.eclipse.paho.android.service:1.1.1'

    // Sceneform (ARCore)
    implementation 'com.google.ar:sceneform:1.17.1'

    // Bluetooth und Sensor (AndroidX)
    implementation 'androidx.core:core-ktx:1.13.1'
    implementation 'androidx.appcompat:appcompat:1.7.0'
}

```
## 7. Start der App (MainActivity)

```kotlin

@AndroidEntryPoint
class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            RadarAppTheme {
                AppNavHost()
            }
        }
        // Service starten (optional automatisch)
        Intent(this, SensorService::class.java).also { startService(it) }
    }
}

```
**AppNavHost** (Navigation):

```kotlin

@Composable
fun AppNavHost() {
    val navController = rememberNavController()
    Scaffold(bottomBar = { BottomNavigationBar(navController) }) {
        NavHost(navController, startDestination = "live") {
            composable("live") { LiveScreen() }
            composable("map") { MapScreen() }
            composable("settings") { SettingsScreen() }
            composable("data") { DataScreen() }
        }
    }
}

```
## 8. Umsetzungshinweise für die echte Hardware (CT45 XP)

- **BLE‑Beacon‑Transmitter**: Der Honeywell‑spezifische Dienst `com.honeywell.df.beacontransmitter` muss über Reflexion aufgerufen werden. Im Code kann dies über `Context.getSystemService()` mit einem kundenspezifischen Service‑Manager erfolgen. Für die Simulation wird ein generischer BLE‑Scanner verwendet.
- **Warm‑Swap‑Handling**: Registriere einen Broadcast‑Receiver für `com.honeywell.batteryswap.STARTED` und `...COMPLETED` im SensorService, um bei Akkuwechsel die Sensoren kurz zu pausieren und die Datenbank zu flushen.
- **Externe Radarmodule**: Die Kommunikation erfolgt über USB‑Seriell (CDC) oder UDP. Verwende die Bibliothek `com.github.mik3y:usb-serial-for-android:3.8.0` für serielle Verbindungen.

## 9. Test und Validierung

- **Simulator‑Modus**: Für Tests ohne echte Sensoren kann ein `MockSensorRepository` bereitgestellt werden, das synthetische Daten (Sinusbewegungen, simulierte Punktwolken) generiert.
- **Unit‑Tests**: Teste den EKF mit vorgegebenen Trajektorien und vergleiche mit Ground‑Truth.
- **Integrationstest**: Auf dem CT45 XP im realen Raum durchführen.

## 10. Abschließende Hinweise

Die obige Projektvorlage ist **direkt umsetzbar** – sie enthält alle notwendigen Architekturelemente, GUI‑Screens, Datenbank‑ und Kommunikationsmodule sowie den Kern‑EKF. Die fehlenden Implementierungsdetails (z. B. vollständige Sensorregistrierung, genaue Kalibrierung, Micro‑Doppler‑Extraktion) sind als Übungsaufgabe oder durch Einbindung vorhandener Bibliotheken (z. B. `libradar` für UWB‑Auswertung) zu ergänzen.

**Nächste Schritte für die Entwicklung**:

1. Android‑Studio‑Projekt erstellen und Abhängigkeiten einbinden.
2. Die Compose‑Screens mit einfachen Platzhaltern umsetzen.
3. Den SensorService mit IMU und BLE grundlegend implementieren.
4. Den EKF integrieren und mit simulierten Daten testen.
5. Die 3D‑Sceneform‑Ansicht einrichten und Punktwolken darstellen.
6. MQTT‑Client für die Datenübertragung hinzufügen.
7. Spezifische CT45‑XP‑APIs (Warm‑Swap, Beacon‑Sender) einbinden.

## 11. Erweiterung: Vom Einzelgerät zum kollaborativen Sensor-Netzwerk (Swarm Mapping / Collaborative SLAM)

Von der **Einzelgeräte-Erfassung** hin zu einem **kollaborativen Sensor-Netzwerk (Swarm Mapping / Collaborative SLAM)**: Die App wird nicht nur zum passiven Empfänger, sondern zum **intelligenten Edge-Controller**, der Netzwerkdaten mehrerer Client-Geräte (weitere CT45 XP, stationäre IoT-Knoten oder Drohnen) fusioniert, um ein vollständiges, lückenloses 3D-Abbild der Umgebung zu erstellen – selbst wenn einzelne Geräte nur Teilbereiche sehen. Hier sind die detaillierten Überlegungen zur **Systemarchitektur, mathematischen Adaption und GUI-Gestaltung** für diese vernetzte, Multi-Client-Erfassung.

### 11.1. Systemarchitektur: Vom Einzelkämpfer zum Schwarm

Die App muss zwischen drei Betriebsmodi unterscheiden, die der Nutzer im Einstellungs-Screen (Screen C) wählen kann:

- **Modus A (Master / Edge-Server):** Dieses CT45 XP fungiert als zentrale Recheneinheit. Es empfängt die Netzwerkdaten aller Slaves, führt die globale Sensorfusion durch und rendert die Gesamtkarte.
- **Modus B (Slave / Client):** Dieses Gerät erfasst nur lokal, führt einen lokalen EKF durch und sendet seine komprimierten Zustandsdaten (Pose, Punktwolke, detektierte Objekte) an den Master.
- **Modus C (Ad-hoc Peer-to-Peer):** Alle Geräte sind gleichberechtigt und tauschen untereinander Daten aus (dezentrales Mapping – hierfür wäre ein DDS-Protokoll besser, aber wir konzentrieren uns auf den Master-Slave-Ansatz, da er recheneffizienter für das CT45 XP ist).

**Datenfluss im Master-Gerät:**

1. **Netzwerk-Layer:** Die App öffnet einen MQTT-Server (oder nutzt den bestehenden Client, um ein Topic zu abonnieren, z. B. `swarm/+/map`).
2. **Empfangspuffer:** Eingehende Netzwerkpakete (Sparkplug B / Protobuf) werden in einen asynchronen Puffer gelegt.
3. **Globale Koordinatenausrichtung (Alignment):** Jeder Client hat seinen eigenen Ursprung (0,0,0). Der Master muss diese lokalen Koordinatensysteme in ein globales System überführen.
4. **Zentrale Fusion (Collaborative EKF / Pose-Graph-Optimierung):** Die transformierten Punktwolken werden zu einer globalen Punktwolke zusammengefügt. Doppelte Objekte (z. B. eine Person, die von zwei Geräten gesehen wird) werden erkannt und zu einem einzigen Avatar mit höherer Konfidenz fusioniert.
5. **Persistenz:** Die fusionierte Karte wird im SQLite-WAL-Cache des Masters gespeichert und periodisch an 3dxStage gesendet.

### 11.2. Mathematische Adaption: Globale Koordinatenfusion (Collaborative EKF)

Das mathematische Kernproblem ist die **Koordinatentransformation**. Jeder Client $i$ sendet seine Objekte im lokalen Koordinatensystem $L_i$. Der Master führt eine **starrkörperliche Transformation** (Rotation $\mathbf{R}_i$ und Translation $\mathbf{t}_i$) durch, um diese in das globale System $G$ zu überführen:

$$\mathbf{p}_G = \mathbf{R}_i \cdot \mathbf{p}_{L_i} + \mathbf{t}_i$$

Wie bestimmt der Master $\mathbf{R}_i$ und $\mathbf{t}_i$ ohne manuelle Kalibrierung? Über **gegenseitige Beobachtungen (Inter-Device Observations)**:

- Wenn Gerät A das Gerät B in seinem BLE- oder UWB-Feld sieht, sendet Gerät A die relative Position von Gerät B an den Master.
- Gleichzeitig sendet Gerät B seine eigene Positionsschätzung.
- Der Master gleicht diese beiden Informationen ab (Umeyama-Algorithmus oder nicht-linearer Least-Squares-Optimierer) und berechnet die exakte Rotations- und Verschiebungsmatrix zwischen den Geräten.

**Kovarianz-Propagation bei Netzwerkverzögerung:** Da Netzwerkdaten asynchron ($\Delta t$ von 50 ms bis 500 ms) eintreffen, muss der Master die Echtzeit-Latenz berücksichtigen. Der empfangene Zustand $\mathbf{x}_k$ wird mittels der Systemübergangsmatrix $\mathbf{F}_k$ auf den aktuellen Master-Zeitstempel $k+\Delta t$ extrapoliert:

$$\mathbf{x}_{k+\Delta t} = \mathbf{F}_{k+\Delta t} \cdot \mathbf{x}_k$$

Die Varianz wird durch Addition des Laufzeit-Jitters erhöht (unscharfes Netzwerkrauschen).

### 11.3. GUI-Adaption: Wie zeigt die App „Mehrere Clientgeräte" an?

Die grafische Benutzeroberfläche muss die Komplexität des Schwarms **absolut intuitiv** darstellen. Hierfür werden die bestehenden Screens wie folgt erweitert:

#### 11.3.A. Live-Dashboard (Screen A) – „Der Schwarm-Kompass"

- **Darstellung der eigenen Position vs. Schwarm:** In der 3D-Szene wird die eigene Position weiterhin als **roter Würfel** dargestellt. Die *anderen* Client-Geräte (Kollegen im Einsatz) werden als **blaue, durchscheinende Würfel** mit einem eindeutigen ID-Label (z. B. „Trupp 2") dargestellt. Verbindet eine **gepunktete grüne Linie** den roten Würfel mit einem blauen Würfel, bedeutet dies: *Diese beiden Geräte sehen sich gegenseitig via UWB/BLE und haben ihre Koordinaten erfolgreich abgeglichen.* Fällt die Linie aus, ist die Netzwerkfusion unsicher.
- **Abdeckungs-Hitzekarte (Overlay):** Der Nutzer kann über einen Schalter eine **2D-Heatmap** über die 3D-Szene legen. Diese zeigt farblich (Rot = hohe Abtastdichte, Blau = niedrige Abtastdichte) an, *wo bereits mehrere Clients gemappt haben*. Das hilft dem Einsatzleiter, gezielt Lücken im Gebäude zu schließen.
- **Client-Status-Chips (neue Zeile unter der Haupt-Statusleiste):** Eine horizontale, scrollbare Liste mit den Avataren der verbundenen Clients.
  - *Grüner Punkt* = Datenstrom aktiv und synchronisiert.
  - *Gelber Punkt* = Verbunden, aber Koordinatenausrichtung läuft noch.
  - *Roter Punkt* = Verbindungsabbruch (Cache-Modus lokal).

#### 11.3.B. Kartierung & Objekte (Screen B) – „Dezentrale Objektfilter"

Die Objektliste (Bottom Sheet) wird um eine entscheidende Spalte erweitert: **„Quelle"**.

- Jeder detektierte Ausgang, jede Person oder jedes Elektrogerät erhält ein kleines **Quell-Icon**:
  - 🟢 **Grüner Kreis** = Objekt wurde von *diesem* Gerät selbst gesehen.
  - 🟣 **Violetter Kreis** = Objekt wurde von einem *anderen* Client gesehen (über Netzwerk empfangen).
  - 🔵 **Blauer Kreis** = Objekt wurde von *mehreren Clients* kollaborativ bestätigt (höchste Konfidenz).

**Zusätzliche Funktion: „Follow-Me"-Fokus:** Klickt der Einsatzleiter in der Liste auf ein Objekt, das von einem anderen Client erfasst wurde, schwenkt die 3D-Ansicht des Masters nicht zu den Koordinaten des Objekts, sondern **zur Position des anderen Clients**, der es sieht – plus einer Blickrichtung (Pfeil), um zu verstehen, *aus welchem Winkel* der Kollege die Wand durchleuchtet.

#### 11.3.C. Historische Daten (Screen D) – „Netzwerk-Session-Playback"

Die Aufzeichnung wird um die Netzwerk-Telemetrie erweitert. Der Export als **JSON/CSV** enthält nun nicht mehr nur eine Trajektorie, sondern ein **verschachteltes Objekt**:

```json
{
  "master_session": "2026-08-11_14-00",
  "clients": [
    { "id": "CT45-01", "trajectory": [...] },
    { "id": "CT45-02", "trajectory": [...] }
  ],
  "fused_map": {
    "points": [...],
    "objects": [...]
  },
  "relative_transformations": [
    { "from": "CT45-01", "to": "CT45-02", "R": [...], "t": [...] }
  ]
}
```

### 11.4. Visualisierung der Netzwerk-Qualität (Einblendung)

Da die Kartenfusion maßgeblich von der Netzwerkstabilität abhängt, führt die App ein **neues, festes Overlay-Element** in der oberen rechten Ecke ein (neben der Micro-Doppler-Signatur):

- Ein **kreisrundes Radar-Diagramm (Spider-Chart)** mit 4 Achsen:
  1. **Bandbreite** (Mbit/s)
  2. **Latenz** (ms)
  3. **Paketverlust** (%)
  4. **Synchronisations-Offset** (cm Abweichung zwischen den Clients)

Sind alle 4 Achsen weit außen (grün), ist die Schwarmfusion perfekt. Bricht eine Achse ein (z. B. hohe Latenz), färbt sich das Diagramm gelb und die App schaltet automatisch in den **„Optimistischen Fusionsmodus"** – sie interpolatiert die fehlenden Client-Daten durch die KI-Hypothesen (Option ④ aus der ersten Abhandlung), bis das Netzwerk sich erholt.

### 11.5. Umsetzungsspezifische Code-Erweiterung (Kotlin)

Um diese Multi-Client-Fusion in der bereitgestellten Clean-Architecture umzusetzen, werden folgende neue Komponenten benötigt:

```kotlin
// 1. Datenmodell für den Netzwerk-Client
data class RemoteClient(
    val deviceId: String,
    val lastSeen: Long,
    val localPose: EKFState,            // Pose im lokalen System
    val globalTransform: RigidTransform, // R und t zum Master
    val pointCloudChunk: List<Point3D>,  // Nur Delta-Updates (sparsam)
    val detectedObjects: List<DetectedObject>
)

// 2. Use Case für die zentrale Fusion (im Master)
class FuseSwarmDataUseCase @Inject constructor(
    private val alignmentCalculator: UmeyamaAlignment, // Least-Squares-Fitter
    private val globalEKF: CollaborativeEKF
) {
    operator fun invoke(clients: List<RemoteClient>, localState: EKFState): FusedMap {
        // 1. Lokale Zustände in globale transformieren
        val transformedClients = clients.map { client ->
            client.copy(
                globalPose = alignmentCalculator.transform(client.localPose, client.globalTransform)
            )
        }
        // 2. Punktwolken zusammenführen & Voxel-Filter anwenden (Redundanz vermeiden)
        val fusedPoints = mergePointClouds(transformedClients)
        // 3. Objekte deduplizieren (gleiche Person, die von 2 Geräten gesehen wird)
        val fusedObjects = deduplicateObjects(transformedClients.flatMap { it.detectedObjects })
        // 4. Update des globalen EKF mit den neuen transformierten Daten
        globalEKF.updateWithSwarm(transformedClients)
        return FusedMap(fusedPoints, fusedObjects, globalEKF.getState())
    }
}

// 3. MQTT-Empfänger im SensorService (neben dem Sender)
private fun setupMqttSubscription() {
    mqttClient.subscribe("swarm/+/pointcloud", 1) { topic, message ->
        val remoteData = SparkplugBParser.parse(message.payload)
        // In Hintergrund-Thread in den Fusions-Puffer legen
        fusionBuffer.add(remoteData)
        // Master-Trigger für Neuberechnung (max. 10 Hz, um Akku zu schonen)
        if (fusionBuffer.size % 3 == 0) {
            viewModel.fuseSwarmData()
        }
    }
}
```

### 11.6. Strategisches Fazit für den Multi-Client-Betrieb

Durch diese Erweiterung wird die App zur **zentralen Einsatz-Leitstelle in der Westentasche**. Die GUI macht unsichtbare Netzwerkprozesse sichtbar:

- Der **Nutzer sieht nicht nur die Wand**, sondern erkennt sofort, *welcher Kollege gerade welche Wand durchleuchtet*.
- Die **farbliche Objektcodierung** (Grün/Blau/Rot) bleibt erhalten, wird aber um den **Source-Indikator** (lokal/remote/fusioniert) ergänzt, um die Vertrauenswürdigkeit der Daten zu signalisieren.
- Selbst bei **vollständigem Netzwerkausfall** bricht das System nicht zusammen – jeder Client läuft im „Offline-First"-Modus (SQLite-WAL) weiter und synchronisiert die aufgenommenen Kartenstücke automatisch nach, sobald die Verbindung zurückkehrt (ähnlich einer Git-Sync-Strategie für 3D-Karten).

Diese Architektur macht das Honeywell CT45 XP zum ultimativen **kollaborativen 3D-Scanner** für Großlagen, bei dem mehrere Trupps simultan ein Gebäude von allen Seiten erfassen und live ein gemeinsames, hochpräzises Lagebild für die 3dxStage-Plattform generieren.

## 12. Bedienkonzept: Hardware-Buttons, Gesten & Kontextuelle Interaktion

Die bisherigen Kapitel beschreiben Architektur und Datenfluss. Dieses Kapitel definiert die **physische und taktile Bedienung** der App – bewusst so gestaltet, dass Einsatzkräfte die App unter realer Belastung (Handschuhe, Dunkelheit, Lärm, Zeitdruck) sicher bedienen können.

### 12.1. Hardware-Button-Mapping (Das CT45 XP als „Taktik-Controller")

Das CT45 XP verfügt über zwei programmierbare Hardware-Seitentasten und einen zentralen Scan-Trigger. Diese müssen **immer** Vorrang vor Touch-Gesten haben (da sie auch bei Regen oder dicken Handschuhen funktionieren).

| Hardware-Taste | Standardbelegung (IDLE) | Belegung im SCANNING-Modus | Haptisches Feedback |
| --- | --- | --- | --- |
| **Linke Seitentaste (oben)** | **„Toggle Ghost-Modus"** – Schaltet die KI-Hypothesen (Option ④) ein/aus. Ist sie *rot hinterleuchtet*, sieht der Nutzer prädiktive Avatare hinter dicken Wänden. | **„Instant Bookmark"** – Setzt einen blauen Ankerpunkt an der aktuellen Position (inkl. eines kurzen Sprach-Diktats via integriertem Mikrofon, z. B. „Hier ist der Hauptgasanschluss"). | 1× langer Vibrationsimpuls (Bestätigung für das Setzen des Ankers) |
| **Rechte Seitentaste (unten)** | **„Center View"** – Resetet die 3D-Kamera auf die eigene Position und Blickrichtung. | **„Force Sync"** – Erzwingt sofort den Upload des lokalen SQLite-WAL-Cache an den Master und alle anderen Clients (ignoriert das „Report-by-Exception"-Prinzip). | 2× kurze Vibrationen (Daten werden jetzt übertragen) |
| **Scan-Trigger (Mitte)** | Startet den SCANNING-Modus. | Stoppt den SCANNING-Modus sicher (flusht zuerst die Datenbank). | Langer, tiefer Ton (Stopp-Signal) |

### 12.2. Die „Kontextuelle Aktionsleiste" (Das Extra-Toolbar-Overlay)

Um den 3D-Bildschirm nicht zuzukleistern, gibt es eine **halbtransparente, einklappbare Schnellleiste** (oben rechts, direkt unter der Statusleiste). Der Nutzer wischt von rechts nach links, um sie auszufahren.

**Die 5 wichtigsten Extra-Buttons im Detail:**

- **🧹 „Rauschen filtern" (Filter)**: Öffnet ein radiales Menu, um Punktwolken zu filtern:
  - *Alle Punkte* / *Nur Wände* / *Nur Personen* / *Nur Elektro (Blau)*.
  - *Sonderfunktion*: „Rauschen ausblenden" – entfernt temporär alle Punkte mit einer Konfidenz `< 60 %`.
- **📡 „Abdeckungs-Hitze" (Heatmap-Toggle)**: Schaltet die Heatmap-Überlagerung ein/aus (siehe Multi-Client-Ausführung, Kapitel 11). Wenn aktiv, wird die Szene in Rot/Blau eingefärbt – perfekt, um zu sehen, *wo der Schwarm noch blinde Flecken hat*.
- **🔒 „Kamera fixieren" (Lock View)**: Friert die 3D-Kamera ein. Der Nutzer kann nun das Gerät in die Tasche stecken, während die App weiter scannt. Ein kleiner grüner Punkt im Rand informiert, dass die Aufnahme läuft.
- **🚨 „Mark as Hazard" (Gefahrenmarkierung)**: Ein roter Button, der *nur* erscheint, wenn der Nutzer in der 3D-Szene ein Objekt *antippt*. Er versieht das Objekt (z. B. das blaue Elektrogerät) mit einem roten Warndreieck, das sofort an *alle anderen Clients* im Netzwerk gesendet wird.
- **📤 „Momentaufnahme teilen" (Share)**: Erstellt nicht nur einen Screenshot, sondern ein *miniaturisiertes 3D-Modell (.glTF)* der aktuellen Ansicht und teilt es per Bluetooth/NFC an ein anderes CT45 XP in der Nähe (AirDrop-Prinzip).

### 12.3. Erweiterte Touch-Gesten (Multitouch & Long-Press)

Die Bedienung über reine Wischgesten entlastet die Buttons und macht die Nutzung intuitiver:

| Geste | Aktion | Visuelles Feedback |
| --- | --- | --- |
| **Doppeltippen (Zwei-Finger)** | **„Teleport zum Ursprung"** – Setzt die 3D-Ansicht zurück auf die Position (0,0,0) des Masters. | Die Szene zoomt kurz heraus und wieder hinein. |
| **Langer Druck (Long-Press) auf einen freien Bereich** | Öffnet das **„Kontextuelle Messwerkzeug"**. Nutzer kann Punkt A und Punkt B antippen – die App berechnet sofort die *Euklidische Distanz* im 3D-Raum ($d = \sqrt{(x_2-x_1)^2 + (y_2-y_1)^2 + (z_2-z_1)^2}$) und blendet sie ein. | Eine grüne Linie erscheint zwischen den Punkten mit dem Meterwert. |
| **Zwei-Finger-Wischen (horizontal)** | **„Zeitachse scrollen"** – Spult die letzten 30 Sekunden der eigenen Trajektorie zurück oder vorwärts (Zeitreise-Modus). Die Punktwolke bewegt sich mit. | Ein gelber Zeitbalken erscheint am unteren Bildschirmrand. |
| **Antippen eines Objekts (Person/Elektro/Ausgang)** | Öffnet das **„Mini-Info-Overlay"** (kein Vollbild). | Ein kleines, rundes Popup erscheint direkt *über* dem Objekt. |

### 12.4. Das „Mini-Info-Overlay" (Aktionen bei Objekt-Antippen)

Wenn der Nutzer eine **grüne Person**, ein **blaues Elektrogerät** oder einen **roten Ausgang** antippt, öffnet sich direkt über dem Objekt ein halbtransparentes Kreis-Menü mit 3 spezifischen Aktionen. Das ist die *intelligenteste* Interaktion der App.

#### 12.4.A. Wenn eine PERSON angetippt wird (Grün)

- **👁️ „Folgen"**: Die 3D-Kamera verfolgt automatisch die Bewegung dieser Person (ideal, um einen fliehenden Verdächtigen im Gebäude zu tracken).
- **📢 „Alarm an Schwarm"**: Sendet einen Ping an alle anderen Clients: *„Person hier – Fokus auf diesen Raum!"*.
- **❤️ „Vitaldaten anzeigen"**: Blendet ein kleines Diagramm mit der aktuellen Atemfrequenz $f_r$ und der Herzfrequenz (falls vom Radar unterstützt) ein.

#### 12.4.B. Wenn ein ELEKTROGERÄT angetippt wird (Blau)

- **⚡ „Stromführend prüfen"**: (Nur wenn ein externes EMF-Messgerät gekoppelt ist). Zeigt an, ob Spannung anliegt.
- **📍 „Als Wegpunkt speichern"**: Speichert die Koordinate als Referenz für die spätere Bauplanung.
- **🚫 „Sperrzone markieren"**: Zeichnet einen roten virtuellen Kreis (Radius 2 m) um das Gerät, um andere Einsatzkräfte vor Explosionsgefahr zu warnen.

#### 12.4.C. Wenn ein AUSGANG angetippt wird (Rot umrahmt)

- **🧭 „Hier führt der Weg raus"**: Die App berechnet den kürzesten Pfad von der aktuellen Position zum Ausgang (unter Berücksichtigung der bekannten Wände) und zeichnet eine **gepunktete grüne Navigationslinie** durch die Szene.
- **📐 „Durchgangsbreite messen"**: Die App schätzt die lichte Weite der Tür anhand der Punktwolke und zeigt an, ob ein Rettungstrage hindurchpasst (`> 0,8 m`).

### 12.5. Aktionen für den Multi-Client-Schwarm (Netzwerkaktionen)

Da die App jetzt Daten mehrerer Geräte fusioniert, braucht es spezielle **„Schwarm-Buttons"** in der oberen Statusleiste:

- **„Master-Übernahme" (Kronen-Symbol)**: Ein Client kann per Knopfdruck die Rolle des Masters übernehmen, falls der ursprüngliche Master ausfällt (Failover).
- **„Follow-the-Leader" (Flaggen-Symbol)**: Aktiviert den *Schwarm-Sync-Modus*. Die Punktwolken aller Clients werden in Echtzeit so transformiert, dass sie exakt auf die Position des *schnellsten* Clients ausgerichtet werden (wichtig bei dynamischen Geisellagen).
- **„Daten-Konsens" (Handshake-Symbol)**: Startet einen manuellen Abgleich. Die App prüft, ob Objekte von mehreren Clients gesehen wurden, und erhöht deren Konfidenzwert – oder löscht sie, wenn sie nur von einem Gerät gesehen wurden (Redundanzprinzip).

### 12.6. Notfall- und „Panic"-Aktionen

Für den absoluten Ernstfall (Stromausfall, akute Gefahr, Gerät stürzt ab):

- **„Emergency Data Flush" (Überall verfügbar)**: Ein *unsichtbarer* Button. Der Nutzer drückt **gleichzeitig** die Lautstärke-hoch + Lautstärke-runter-Taste für 3 Sekunden. Die App erzwingt sofort einen `PRAGMA wal_checkpoint(TRUNCATE)` auf die SQLite-Datenbank, schreibt alle Metadaten in einen verschlüsselten `.enc`-Container und sendet ein **„Mayday"-Signal** mit den letzten bekannten Koordinaten an die Einsatzleitung.
- **„Man-Down" (Sturz-/Reglos-Erkennung)**: Die App nutzt die IMU. Erkennt sie, dass das CT45 XP für `> 10 Sekunden` regungslos im 90°-Winkel (liegend) verharrt, *während* der SCANNING-Modus aktiv ist, löst sie automatisch einen akustischen Alarm (über den Lautsprecher) und sendet eine **Notfall-Push-Nachricht** an den Master-Client.

### 12.7. Sprachsteuerung (Hands-Free – optional aber zukunftsweisend)

Da die Hände oft mit Werkzeug oder Waffen belegt sind, wird eine **Wake-Word**-Steuerung integriert (nur im Master-Modus aktiv, um Akku zu sparen):

- *„CT45 – Markiere Position"* → Setzt einen Bookmark.
- *„CT45 – Zeige Ausgänge"* → Blendet nur die roten Umrandungen ein, alle anderen Objekte werden transparent.
- *„CT45 – Sync jetzt"* → Erzwingt den Netzwerk-Upload.

### 12.8. Zusammenfassende Bedientabelle für den Nutzer (Quick-Reference)

| Deine Aktion | Systemreaktion | Einsatzszenario |
| --- | --- | --- |
| **Linke Taste drücken** | Setzt einen blauen Ankerpunkt im 3D-Raum. | Du findest einen versteckten Hydranten und markierst ihn. |
| **Auf eine Person tippen + „Folgen"** | Kamera trackt die Person hinter der Wand. | Der Geiselnehmer bewegt sich – du behältst ihn im Blick. |
| **Zwei-Finger-Horizontal-Wisch** | Zeitreise durch die eigene Laufstrecke. | Du willst prüfen, ob die Wand vor 2 Minuten schon da war. |
| **Lautstärke +/- gleichzeitig 3 s halten** | Notfall-Flush & Mayday-Signal. | Das Gebäude stürzt ein – die letzten Messdaten müssen gerettet werden. |
| **Rechte Taste + Scannen (Kombi)** | Erzwingt sofortige Netzwerksynchronisation. | Das WLAN bricht gleich ab – schnell noch die Rohdaten zum Master schicken! |

**Fazit zur Bedienung**

Diese Überlegungen transformieren das Honeywell CT45 XP von einem „Messgerät" zu einem **digitalen Spiegel der Einsatzlage**. Die Bedienung ist nicht mehr linear, sondern **radial und kontextuell**:

1. **Hardware** übernimmt die Sicherheitsfunktionen (Stopp, Sync, Markierung).
2. **Gesten** steuern die räumliche Wahrnehmung (Zoom, Zeitreise, Messung).
3. **Objekte antippen** öffnet die taktischen Optionen (Verfolgen, Warnen, Navigieren).

Diese Dreiteilung stellt sicher, dass der Nutzer in *jeder* Situation – ob im laufenden Gefecht, bei akustischem Lärm oder bei Dunkelheit – die App blind (nur durch Vibration und Ton) sicher bedienen kann. Die GUI bleibt dabei aufgeräumt, da 80 % der Aktionen *unsichtbar* über Gesten und Hardware laufen und nur die wichtigsten 20 % als Extra-Buttons eingeblendet werden.