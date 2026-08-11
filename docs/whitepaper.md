# Hochauflösende 3D-Raumerfassung und Wanddurchdringung mittels Honeywell CT45 XP

**Systemarchitektur, mathematische Sensorfusion und regulatorische Praxis in Deutschland**

---

## Inhaltsverzeichnis

1. [Technologische Grundlagen und Hardware-Schnittstellen des Honeywell CT45 XP](#1-technologische-grundlagen-und-hardware-schnittstellen)
2. [Die fünf technischen Realisierungsmöglichkeiten zur virtuellen Abbildung](#2-die-fünf-technischen-realisierungsmöglichkeiten)
3. [Physikalische Dämpfung und mathematische Sensorfusion](#3-physikalische-dämpfung-und-mathematische-sensorfusion)
4. [Regulatorische Rahmenbedingungen in Deutschland](#4-regulatorische-rahmenbedingungen-in-deutschland)
5. [Fünf praxisnahe Anwendungsszenarien in Deutschland](#5-fünf-praxisnahe-anwendungsszenarien)
6. [Fazit und strategische Empfehlungen](#6-fazit-und-strategische-empfehlungen)

---

<a id="1-technologische-grundlagen-und-hardware-schnittstellen"></a>
## 1. Technologische Grundlagen und Hardware-Schnittstellen des Honeywell CT45 XP

Die Realisierung einer hochpräzisen, mobilen 3D-Raumerfassung und der Detektion von Objekten sowie Personen hinter opaken Hindernissen erfordert eine tiefe Integration von Hard- und Softwarekomponenten auf einer robusten Embedded-Plattform. Die technische Basis dieses Systems stellt das Honeywell CT45 XP dar, welches auf der Mobility Edge-Produktfamilie aufbaut und durch einen Qualcomm Snapdragon 662 Octa-Core-Prozessor betrieben wird. Über den systemseitigen Entwicklungsmodus erhält das Anwendungsprogramm direkten Zugriff auf Low-Level-Schnittstellen des Linux-Kernels, das Android-Sicherheits-Framework und die hardwareseitige Sensorik des Geräts.

Das Mobilgerät verfügt über eine integrierte inertiale Messwerterfassung (Inertial Measurement Unit – IMU), die sich aus einem hochpräzisen 3-Achsen-Beschleunigungssensor, einem Gyroskop und einem 3D-Magnetometer zusammensetzt. Diese Sensoren liefern Rohdaten mit hohen Abtastraten (typischerweise im Bereich von $100\text{ Hz}$ bis $200\text{ Hz}$), die für die lokale Bewegungsschätzung unerlässlich sind. Die Modellierung der räumlichen Orientierung basiert auf der mathematischen Fusion dieser inertialen Sensorströme, um den kumulativen Drift bei der Koppelnavigation (Dead Reckoning) zu minimieren.

Eine wesentliche Besonderheit des Honeywell CT45 XP ist die Integration eines sekundären, hardwareseitigen Bluetooth-Low-Energy-Senders (BLE), der über den systemeigenen Dienst `com.honeywell.df.beacontransmitter` gesteuert wird. Dieser Sekundär-Transmitter arbeitet hochgradig energieeffizient und sendet selbst dann periodische Beacons aus, wenn das Gerät heruntergefahren oder der Hauptakku weitgehend entladen ist. Für das Triangulationskonzept wird ein zweiter Akkumulator genutzt, welcher ebenfalls mit einem aktiven, programmierbaren BLE-Token (beispielsweise auf Basis eines nRF52810-SoCs) bestückt ist. Dieser zweite Akku dient als stationärer oder mobiler Referenzpunkt im Raum. Das CT45 XP erfasst kontinuierlich die Signalstärke (Received Signal Strength Indicator – RSSI) und, sofern vom BLE-Controller unterstützt, den Einfallswinkel (Angle of Arrival – AoA) des Tokens, um eine präzise relative Positionsbestimmung im Raum vorzunehmen.

Aufgrund der hohen Rechenintensität der Sensorfusions-Algorithmen und der kontinuierlichen Funkaktivität ist das thermische und energetische Management der Plattform kritisch. Der im CT45 XP verbaute smarte Li-Ion-Akkumulator ($3{,}85\text{ V}$, $4020\text{ mAh}$) verfügt über integrierte Diagnosewerkzeuge, die über das Android Battery-Management-Framework ausgelesen werden können. Für einen unterbrechungsfreien Betrieb sorgt das hardwareseitige Warm-Swap-Feature. Wenn der Anwender den Akku im laufenden Betrieb wechselt, löst das Betriebssystem den geordneten, systemweiten Broadcast-Intent `com.honeywell.batteryswap.STARTED` aus. Ein im Hintergrund operierender Dienst fängt diesen Intent ab, schließt offene Dateihandles, stoppt die seriellen Kommunikations-Threads zu den externen Sensoren und erzwingt einen synchronen Flush des Write-Ahead-Logs (WAL) der lokalen SQLite-Datenbank. Die Stromversorgung des flüchtigen RAMs und der inertialen Basissensorik wird während des Tauschs temporär durch eine interne Backup-Batterie aufrechterhalten, die über den Hauptakku geladen wird. Nach dem Einsetzen des neuen Akkumulators signalisiert der Intent `com.honeywell.batteryswap.COMPLETED` den Abschluss, woraufhin die Sensorschnittstellen nahtlos reaktiviert werden, ohne dass ein zeitaufwendiger Systemneustart erforderlich ist.

Die nachfolgende Tabelle fasst die exakten Spezifikationen der für das System relevanten Hardware- und Sensorschnittstellen des Honeywell CT45 XP zusammen:

| Spezifikationsparameter | Technische Ausprägung und Schnittstellenwerte | Relevanz für das Fusionssystem |
| --- | --- | --- |
| SoC-Plattform | Qualcomm Snapdragon 662 (Mobility Edge) | Primäre Recheneinheit für die Sensorfusion und EKF |
| Betriebssystem | Android (mit vollem Zugriff auf API Level 30+) | Ausführung des Tiny Agent-Dienstes und SQLite-WAL |
| Integrierte IMU | Beschleunigungssensor, Gyroskop, Magnetometer, eCompass | Liefert hochfrequente Orientierungs- und Bewegungsdaten |
| Bluetooth / BLE | V5.1 mit sekundärem BLE-Beacon-Transmitter | Lokalisierung und Empfang von Triangulationsdaten |
| Smart Battery | Li-Ion, $3{,}85\text{ V}$, $4020\text{ mAh}$ mit Diagnosesystem | Telemetrieüberwachung zur Vermeidung thermischer Überlastung |
| Energy Management | Physischer Warm-Swap mit Intent-Signalisierung | Ermöglicht unterbrechungsfreie 3D-Scans bei Akkuwechsel |
| Kamerasystem | 13-Megapixel-Rückkamera, 8-Megapixel-Frontkamera | Erfassung visueller Feature-Points für SLAM-Verfahren |

---

<a id="2-die-fünf-technischen-realisierungsmöglichkeiten"></a>
## 2. Die fünf technischen Realisierungsmöglichkeiten zur virtuellen Abbildung

Um auf Basis der beschriebenen Hardware-Infrastruktur ein detailliertes dreidimensionales Abbild der Umgebung und der darin befindlichen Objekte oder Personen zu generieren und in der Virtual-Reality-Plattform 3dxStage (Dassault Systèmes 3DEXPERIENCE) darzustellen, existieren fünf technologische Ansätze. Diese unterscheiden sich grundlegend hinsichtlich ihrer Sensordynamik, der rechnerischen Komplexität und der physikalischen Einschränkungen.

### ① BLE-Signal-Triangulation und 3D-Trajektorienmodell

Dieser rein funkbasierte Ansatz nutzt die gemessene Signalstärke (RSSI) des im Raum platzierten zweiten Akkumulators mit integriertem BLE-Token. Da RSSI-Werte in Innenräumen aufgrund von Mehrwegeausbreitung (Multipath Fading), Reflexionen an Wänden und Abschattungen durch den menschlichen Körper massiv schwanken, ist eine direkte Abstandsschätzung über das Freiraumdämpfungsmodell ungenau. Zur Kompensation fusioniert der adaptive Tiny Agent die RSSI-Rohdaten mit den kontinuierlichen Winkelsatzänderungen des Gyroskops und den Ausrichtungswerten des 3D-Magnetometers. Durch die Integration dieser Bewegungsdaten über ein kinematisches Zustandsmodell wird eine geglättete Zustandstrajektorie des Benutzers im Raum geschätzt. Die Darstellung in 3dxStage beschränkt sich hierbei auf statistische Wahrscheinlichkeitszonen (Konfidenzellipsen); eine tatsächliche Erfassung von Geometrien oder Personen hinter opaken Wänden ist mit diesem Verfahren allein physikalisch ausgeschlossen.

### ② Visuelles und inertiales SLAM in offenen Raumbereichen

Bei diesem Verfahren fungiert die integrierte 13-Megapixel-Rückkamera des Honeywell CT45 XP als optischer Sensor zur Erfassung markanter Geometrien im Sichtbereich (Line-of-Sight – LOS). Über Algorithmen des visuellen SLAM (Simultaneous Localization and Mapping) werden kontrastreiche Punktmerkmale (Feature Points) in aufeinanderfolgenden Kamerabildern detektiert und getrackt. Die inhärente Mehrdeutigkeit der Skalierung bei monokularen Kamerasystemen wird durch die hochfrequenten Beschleunigungsdaten der internen IMU aufgelöst. Das Ergebnis ist eine hochauflösende 3D-Punktwolke der sichtbaren Raumstruktur. Dieses Verfahren liefert exzellente Geometriedaten für offene Bereiche, scheitert jedoch vollständig an visuell opaken Hindernissen wie Trockenbau- oder Ziegelwänden, da keine elektromagnetischen Signale im optischen Spektrum diese Barrieren durchdringen können.

### ③ Sensor-Fusion mit externen Durch-Wand-Radarsystemen

Um eine reale Detektion und räumliche Abbildung von Personen und Objekten hinter Wänden zu ermöglichen, muss das Honeywell CT45 XP über seine USB-C-Schnittstelle oder ein lokales drahtloses Netzwerk (Wi-Fi 6) mit externen Radarmodulen gekoppelt werden. Hierzu werden kompakte Ultra-Breitband-Radarsensoren (UWB, z. B. im Frequenzbereich von $3{,}1\text{ GHz}$ bis $10{,}6\text{ GHz}$) oder Millimeterwellen-Sensoren (mmWave, z. B. im Frequenzbereich um $60\text{ GHz}$) als Zusatzgeräte an der Wand positioniert. Das CT45 XP übernimmt in dieser Architektur die Rolle des zentralen Edge-Controllers, steuert die Radar-Abtastraten, empfängt die vorverarbeiteten Kanal-Impulsantworten (Channel Impulse Response – CIR) und führt die räumliche Zuordnung der Reflexionspunkte durch. Durch diese Sensorfusion können Bewegungen und Atemsignale von Personen hinter Hindernissen exakt geortet und als dynamische 3D-Koordinaten an die Visualisierungsplattform übertragen werden.

### ④ Hypothesen-basiertes 3D-Modelling (KI-gestützt)

Sollte der Einsatz externer Radarsensoren aufgrund von Gewichtsbeschränkungen oder taktischen Vorgaben nicht möglich sein, greift das System auf eine KI-gestützte Inferenz zurück. Der auf dem Snapdragon 662 laufende Tiny Agent wertet die lokalen Bewegungsmuster des Benutzers, die Raumakustik und historische Raumgeometrien aus. Basierend auf diesen unvollständigen Daten generiert ein tiefes neuronales Netzwerk (z. B. ein Convolutional LSTM) plausible Hypothesen über den wahrscheinlichen Aufbau des dahinterliegenden Raums und die Positionierung von Objekten. Die mathematische Grundlage bilden statistische Verteilungsmodelle aus ähnlichen Gebäudeklassen (z. B. Bürogebäude, Wohnungen). In der Visualisierungsumgebung 3dxStage werden diese plausiblen, aber synthetisch generierten Avatare und Einrichtungsgegenstände dargestellt. Es handelt sich hierbei um eine prädiktive Simulation und nicht um eine physikalische Echtzeitmessung.

### ⑤ Hybrid-Mapping: Digitaler Zwilling und Echtzeit-Status-Overlay

Dieser Ansatz setzt die Existenz eines vorgefertigten, digitalen 3D-Gebäudemodells voraus, beispielsweise im CAD- oder BIM-Format (Building Information Modeling). Das Honeywell CT45 XP dient in diesem Szenario als hochpräziser Navigations- und Lokalisierungs-Client im realen Raum. Über eine Kombination aus BLE-Triangulation, GNSS-Empfang im Außenbereich und IMU-Koppelnavigation im Innenbereich wird die Position des Anwenders kontinuierlich mit dem digitalen Koordinatensystem des Gebäudemodells abgeglichen. Die Visualisierungssoftware 3dxStage projiziert dieses statische Geometriemodell als digitalen Zwilling auf das Display des Anwenders. Reale Veränderungen hinter Wänden können jedoch nur erfasst werden, wenn das System Zugriff auf stationäre IoT-Sensoren oder Raumüberwachungskameras des Gebäudes hat, deren Daten als dynamisches Status-Overlay in die 3D-Szene eingeblendet werden.

Die nachfolgende Tabelle vergleicht die fünf Realisierungsoptionen im direkten technologischen Vergleich:

| Realisierungsmethode | Physikalische Durchdringung | Erfassungsgenauigkeit (typisch) | Hardware-Aufwand | Rechenleistung auf Edge-Gerät | Eignung für dynamische Objekte |
| --- | --- | --- | --- | --- | --- |
| ① BLE-Triangulation + IMU | Nein (nur Freiraum-Dämpfung) | $1{,}0\text{ m}$ bis $3{,}0\text{ m}$ | Extrem gering (nur BLE-Token) | Gering (linearer Kalman-Filter) | Eingeschränkt (hohe Latenz) |
| ② Vision- + IMU-SLAM | Nein (Sichtlinie erforderlich) | $0{,}05\text{ m}$ bis $0{,}2\text{ m}$ | Gering (nur integrierte Optik) | Sehr hoch (Feature-Tracking) | Gut (nur im Sichtbereich) |
| ③ Externe Sensor-Fusion | **Ja** (materialabhängig) | $0{,}1\text{ m}$ bis $0{,}3\text{ m}$ | Hoch (UWB- / mmWave-Radar) | Mittel bis hoch (FFT & Filterung) | **Hervorragend** (Echtzeit-Tracking) |
| ④ KI-basierte Hypothesen | Virtuell (prädiktiv) | Statistische Schätzung | Gering (reines Softwaremodell) | Extrem hoch (Inferenz-Latenz) | Befriedigend (statistische Pfade) |
| ⑤ Hybrid-BIM-Mapping | Virtuell (vorgefertigt) | $0{,}5\text{ m}$ bis $1{,}5\text{ m}$ | Mittel (BIM-Daten erforderlich) | Mittel (Modell-Rendering) | Gut (über externe IoT-Datenströme) |

---

<a id="3-physikalische-dämpfung-und-mathematische-sensorfusion"></a>
## 3. Physikalische Dämpfung und mathematische Sensorfusion

Die physikalische Detektion von Objekten and lebenden Personen hinter Wänden erfordert eine mathematisch exakte Modellierung der Interaktion elektromagnetischer Wellen mit verschiedenen Baustoffen. Während optische Sensoren (wie das LiDAR-System des Mobilgeräts) an der Wandoberfläche vollständig reflektiert werden, dringen UWB- und mmWave-Signale tiefer in die Materie ein. Die Ausbreitung wird durch die Permittivität $\varepsilon$ und den Verlusttangens $\tan\delta$ des jeweiligen Mediums bestimmt. Höhere Frequenzen erfahren eine drastisch stärkere Absorption. So weist ein mmWave-Signal bei $60\text{ GHz}$ beim Durchqueren einer gewöhnlichen $12\text{ mm}$ starken Gipskartonwand eine Signalretention von etwa $50\ \text{\%}$ auf, während ein $150\text{ mm}$ dicker Stahlbetonpfeiler das Signal fast vollständig blockiert (Retention $< 10\ \text{\%}$). Niedrigfrequentere UWB-Signale im Bereich von $3\text{ GHz}$ bis $5\text{ GHz}$ bieten hier eine deutlich höhere Eindringtiefe und sind robuster gegenüber dicken Barrieren.

Die rechnerische Zusammenführung dieser stark rauschbehafteten und unvollständigen Sensorsignale erfolgt über einen adaptiven Extended Kalman Filter (EKF), der als zentraler Algorithmus im Tiny Agent implementiert ist. Der kontinuierliche Zustandsvektor des Systems zu einem diskreten Zeitschritt $k$ wird als 6-DOF-Zustand (Degrees of Freedom) modelliert:

$$\mathbf{x}_k = \begin{bmatrix} x_k & y_k & z_k & v_{x,k} & v_{y,k} & v_{z,k} \end{bmatrix}^T$$

wobei $x_k, y_k, z_k$ die räumliche Position im dreidimensionalen Koordinatensystem beschreiben und $v_{x,k}, v_{y,k}, v_{z,k}$ die entsprechenden Geschwindigkeitskomponenten darstellen. Die zeitliche Prädiktion des Zustands basiert auf einem kinematischen Modell mit konstanter Geschwindigkeit:

$$\mathbf{x}_{k\vert k-1} = \mathbf{F}_k \mathbf{x}_{k-1\vert k-1} + \mathbf{w}_k$$

Die Systemübergangsmatrix $\mathbf{F}_k$ ist definiert als:

$$\mathbf{F}_k = \begin{bmatrix} \mathbf{I}_{3\times3} & \Delta t \cdot \mathbf{I}_{3\times3} \\ \mathbf{0}_{3\times3} & \mathbf{I}_{3\times3} \end{bmatrix}$$

wobei $\mathbf{I}_{3\times3}$ die Einheitsmatrix und $\Delta t$ das zeitliche Intervall zwischen zwei Filterschritten (typischerweise $50\text{ ms}$ bei einer Update-Rate von $20\text{ Hz}$) repräsentiert. Das Prozessrauschen wird durch die Kovarianzmatrix $\mathbf{Q}_k$ abgebildet.

Bei der Akquisition neuer Messdaten von den physischen Sensoren (UWB-Radar, mmWave-Sensor, LiDAR und BLE-RSSI) führt der EKF einen Messwert-Aktualisierungsschritt durch:

$$\mathbf{x}_{k\vert k} = \mathbf{x}_{k\vert k-1} + \mathbf{K}_k \left( \mathbf{z}_k - \mathbf{h}(\mathbf{x}_{k\vert k-1}) \right)$$

Die Dynamik dieses Schrittes wird maßgeblich durch den Kalman-Gain $\mathbf{K}_k$ gesteuert, der das Verhältnis zwischen der Prädiktions-Fehlerkovarianz $\mathbf{P}_{k\vert k-1}$ und der Messrausch-Kovarianzmatrix $\mathbf{R}_k$ beschreibt:

$$\mathbf{K}_k = \mathbf{P}_{k\vert k-1} \mathbf{H}_k^T \left( \mathbf{H}_k \mathbf{P}_{k\vert k-1} \mathbf{H}_k^T + \mathbf{R}_k \right)^{-1}$$

Der Tiny Agent passt die diagonale Messrausch-Kovarianzmatrix $\mathbf{R}_k$ in Echtzeit an die physikalischen Umgebungsbedingungen an:

$$\mathbf{R}_k = \text{diag}(\sigma^2_{\text{Lidar},k}, \sigma^2_{\text{mmWave},k}, \sigma^2_{\text{UWB},k}, \sigma^2_{\text{BLE},k})$$

Registriert der optische Sensor oder das LiDAR-System des Mobilgeräts beispielsweise eine signifikante Dämpfung durch Staub- oder Rauchentwicklung (Scattering), erhöht der Tiny Agent den Rauschparameter $\sigma^2_{\text{Lidar},k}$ gegen Unendlich ($\sigma^2_{\text{Lidar},k} \to \infty$). Dadurch sinkt der entsprechende Eintrag im Kalman-Gain auf Null, und das System ignoriert den LiDAR-Datenstrom vollständig. Stattdessen stützt sich der Filter primär auf die dämpfungsfreien UWB- und mmWave-Datenströme, deren Rauschvarianzen $\sigma^2_{\text{UWB},k}$ und $\sigma^2_{\text{mmWave},k}$ auf Basis der aktuellen Signal-Rausch-Verhältnisse (Signal-to-Noise Ratio – SNR) minimal gehalten werden.

Zur Erkennung von lebenden Personen hinter opaken Wänden wertet der UWB-Prozessor die Kanal-Impulsantworten (CIR) über eine Micro-Doppler-Analyse aus. Winzige periodische Verschiebungen der Signalphase, die durch die Atembewegung des menschlichen Brustkorbs (im Bereich von wenigen Millimetern) verursacht werden, erzeugen eine charakteristische Frequenzkomponente im empfangenen Signal. Durch eine zeitliche Fourier-Transformation (FFT) über ein gleitendes Fenster von 256 Samples extrahiert das System diese Atemfrequenz $f_r$ im physiologischen Bereich:

$$0{,}15\text{ Hz} \le f_r \le 0{,}6\text{ Hz}$$

Wird eine solche Signatur stabil detektiert, deklariert der Tiny Agent die Position des entsprechenden Reflexionspunkts als "aktiven Person-Avatar" und persistiert diese Information im Cache.

Die folgende Tabelle verdeutlicht die Dämpfungswerte und die verbleibende Signalstärke elektromagnetischer Wellen beim Durchdringen typischer Wandmaterialien in Deutschland bei unterschiedlichen Frequenzen:

| Wandmaterial | Typische Wandstärke | Dämpfung mmWave (60 GHz) | Signalretention mmWave | Dämpfung UWB (5 GHz) | Signalretention UWB | Technischer Effekt auf EKF |
| --- | --- | --- | --- | --- | --- | --- |
| Sicherheitsglas | $10\text{ mm}$ | $\sim 1{,}5\text{ dB}$ | $\sim 70{-}80\ \text{\%}$ | $< 0{,}5\text{ dB}$ | $\sim 90{-}95\ \text{\%}$ | Minimaler Drift; mmWave liefert hohe Winkelauflösung |
| Gipskarton | $12\text{ mm}$ | $\sim 3{,}0\text{ dB}$ | $\sim 50\ \text{\%}$ | $\sim 1{,}0\text{ dB}$ | $\sim 80{-}85\ \text{\%}$ | Teilweise Streuung; EKF fusioniert beide Kanäle |
| Ziegelmauerwerk | $200\text{ mm}$ | $> 10{,}0\text{ dB}$ | $\sim 10{-}20\ \text{\%}$ | $\sim 4{,}0\text{ dB}$ | $\sim 40{-}50\ \text{\%}$ | mmWave stark gedämpft; UWB übernimmt die Führung |
| Stahlbeton | $150\text{ mm}$ | $> 20{,}0\text{ dB}$ | $< 10\ \text{\%}$ | $\sim 8{,}0\text{ dB}$ | $\sim 15{-}25\ \text{\%}$ | mmWave-Ausfall; UWB-Doppler detektiert nur Vitaldaten |

---

<a id="4-regulatorische-rahmenbedingungen-in-deutschland"></a>
## 4. Regulatorische Rahmenbedingungen in Deutschland

Der Betrieb hochfrequenter Funk- und Radarsysteme im öffentlichen und industriellen Raum ist in Deutschland streng reglementiert. Die zuständigen Behörden sind die Bundesnetzagentur (BNetzA) für die Frequenzallokation und das Bundesamt für Sicherheit in der Informationstechnik (BSI) für die Einhaltung von Cybersicherheits-Standards.

### Frequenzregulierung für Ultra-Breitband (UWB) und Millimeterwelle (mmWave)

Für den Betrieb von UWB-Geräten hat die BNetzA allgemeine Frequenzzuteilungen für Kurzstreckenfunk (Short Range Devices – SRD) erlassen. Im Frequenzbereich von $3{,}1\text{ GHz}$ bis $10{,}6\text{ GHz}$ darf die maximale spektrale Sendeleistungsdichte einen Grenzwert von $-41{,}3\text{ dBm/MHz}$ (EIRP) nicht überschreiten, um Störungen koexistierender Primärdienste (wie Mobilfunk oder Wetterradar) auszuschließen. Zudem müssen Geräte, die in bestimmten Teilbändern operieren, Schutzmechanismen wie LDC (Low Duty Cycle) und DAA (Detect and Avoid) implementieren, welche die Sendezeit bei Erkennung anderer Funksignale automatisch reduzieren.

Im mmWave-Bereich, speziell im Frequenzband von $24{,}25\text{ GHz}$ bis $27{,}5\text{ GHz}$ (das sogenannte 26-GHz-Band), vergibt die BNetzA seit 2021 lokale Frequenzzuteilungen für private Campusnetze. Diese Lizenzen sind ideal für industrielle Anwendungen, da sie exklusive Frequenzbereiche mit bis zu $800\text{ MHz}$ Bandbreite zur Verfügung stellen. Die jährliche Gebühr für diese Zuteilungen wird nach einer festgelegten Gebührenformel berechnet, die sich an der zugewiesenen Bandbreite ($B$), der beantragten Fläche ($a$), der Laufzeit ($t$) und der geografischen Lage orientiert, wobei reine Indoor-Anwendungen aufgrund des geringeren Interferenzrisikos bevorzugt zugeteilt werden. Diese Zuteilungen sind zeitlich befristet, maximal jedoch bis zum 31. Dezember 2040 ausgelegt.

### Compliance-Anforderungen nach NIS-2 und dem KRITIS-Dachgesetz

Systeme, die zur Überwachung, Absicherung oder Strukturkartierung im Bereich kritischer Infrastrukturen (KRITIS), wie z. B. Energieversorgung, Transport oder öffentliche Sicherheit, eingesetzt werden, müssen den Anforderungen der europäischen NIS-2-Richtlinie und des deutschen IT-Sicherheitsgesetzes entsprechen. Die Betreiber solcher Systeme sind verpflichtet, strenge Governance- und Risikomanagement-Prozesse zu etablieren und Sicherheitsvorfälle unverzüglich an das BSI zu melden. Relevante Akteure müssen sich bis spätestens März 2026 im nationalen NIS-2-Register registrieren. Das Honeywell CT45 XP erfüllt diese Compliance-Anforderungen auf Geräteebene durch das integrierte Mobility Edge Enterprise Security Framework, welches sicheres Booten (Secure Boot), eine lückenlose Verschlüsselung des lokalen Speichers und die Isolierung kritischer kryptografischer Schlüssel in einem hardwarebasierten Keystore unterstützt.

### Industrielle Datenübertragung und Schnittstellen-Kopplung

Für die Übertragung der erfassten 3D-Geometriedaten und Personentrajektorien an die Plattform 3dxStage nutzt die Applikation auf dem CT45 XP eine zweistufige Kommunikationsarchitektur, bestehend aus OPC UA (Open Platform Communications Unified Architecture) und MQTT (Message Queuing Telemetry Transport). OPC UA stellt hierbei das semantische Informationsmodell bereit. Ein detektiertes Objekt wird im Adressraum als strukturierter Knotentyp definiert, der neben den geometrischen Dimensionen auch Zustandsdaten, Zeitstempel und Konfidenzwerte enthält.

Um diese reichhaltigen Daten effizient über drahtlose Verbindungen (z. B. Wi-Fi 6 oder private 5G-Campusnetze) zu transportieren, werden die OPC-UA-Datenpakete in schlanke MQTT-Nachrichten übersetzt. Hierbei kommt der Sparkplug B-Standard zum Einsatz, welcher ein hocheffizientes, binäres Protokoll-Buffer-Format (Protobuf) vorschreibt und eine eindeutige Topic-Struktur definiert. Durch das Report-by-Exception-Prinzip von MQTT werden Daten nur dann gesendet, wenn eine signifikante Positionsänderung oder ein neues Objekt detektiert wird, was die Netzwerkauslastung minimiert und die Batterielaufzeit des Mobilgeräts schont.

---

<a id="5-fünf-praxisnahe-anwendungsszenarien"></a>
## 5. Fünf praxisnahe Anwendungsszenarien in Deutschland

Die Implementierung einer dedizierten Applikation auf dem Honeywell CT45 XP, die einen selbstverwalteten, persistenten Cache auf Basis der SQLite-WAL-Architektur (Cognitum Seed) nutzt, ermöglicht die Umsetzung hochgradig spezialisierter Einsatzszenarien für verschiedene professionelle Anwendergruppen in Deutschland. Der persistente Speicher dient dabei als Langzeit-Kontexthirn, das Raumstrukturen über mehrere Begehungen hinweg fusioniert und Drift-Effekte eliminiert.

### Szenario 1: Taktische Einsatzbesprechung und Erkundung (Behörden und Organisationen mit Sicherheitsaufgaben – BOS)

Bei polizeilichen Zugriffsszenarien oder Geisellagen in unstrukturierten Gebäudekomplexen stehen Einsatzkräfte oft vor dem Problem fehlender oder veralteter Baupläne. Ein Erkundungstrupp begeht das Vorfeld des Zielobjekts und führt das Honeywell CT45 XP mit sich. Der zweite Akkumulator mit dem aktiven BLE-Token wird als temporärer Ankerpunkt im sicheren Rückraum platziert. Während der Begehung erfasst das visuelle SLAM-System des CT45 XP die Außenfassade und Eingänge des Gebäudes. Gleichzeitig durchdringt das angekoppelte UWB-Radar die Außenwand und detektiert die Bewegungen und Atemfrequenzen von Personen im Inneren.

Der integrierte Tiny Agent fusioniert diese Daten in Echtzeit und speichert die Feature-Points in der lokalen, verschlüsselten SQLite-Datenbank. Bei einem Verbindungsabriss im dichten Gebäudeinneren läuft die Erfassung autark im lokalen WAL-Cache weiter. Sobald die Datenverbindung wiederhergestellt ist, synchronisiert sich der Cache via MQTT Sparkplug B mit dem Einsatzleitwagen. In der 3dxStage-Oberfläche der Einsatzleitung entsteht ein vollständiges, dreidimensionales Lagebild, in dem die Geiseln und Entführer als dynamische Avatare hinter den virtuellen Wänden dargestellt werden, was eine präzise Planung von Zugriffswegen unter Umgehung von Sichtachsen ermöglicht.

### Szenario 2: Gefahren- und Evakuierungssimulation im Brandfall (Universitäten und Feuerwehren)

Zur Optimierung von Brandschutzkonzepten in komplexen öffentlichen Gebäuden, wie Universitätsbibliotheken oder Messehallen, führen Sicherheitsingenieure simulierte Evakuierungsläufe unter realen Bedingungen durch. Das CT45 XP wird in Kombination mit einem externen thermischen Infrarotsensor und einem mmWave-Radar betrieben. Bei starker Rauchentwicklung in den Fluchtwegen wird die optische Kamera des Mobilgeräts unbrauchbar. Der Tiny Agent erkennt diese sensorische Degradation adaptiv und skaliert die LiDAR-Rauschparameter im EKF hoch, während das mmWave-Radar, welches Rauchpartikel dämpfungsfrei durchdringt, als primäre Datenquelle priorisiert wird.

Die Testpersonen bewegen sich durch das verrauchte Szenario; das System erfasst deren genaue Trajektorien sowie die thermische Belastung im Fluchtweg. Der persistente Kontextspeicher auf dem CT45 XP gleicht diese Live-Trajektorien mit dem im Vorfeld geladenen BIM-Modell des Gebäudes ab, um akkumulierte Drifts bei der Lokalisierung zu korrigieren. Die fusionierten Datenströme werden über ein lokales 5G-Campusnetzwerk an die Simulationsplattform 3dxStage übertragen. Hierdurch können Wissenschaftler und Brandschutzbehörden Engpässe, Sichtweitenverluste und Fluchtwegblockaden unter realistischen Bedingungen analysieren, um Fluchtwegeffektivitäten mathematisch exakt nachzuweisen.

### Szenario 3: Bauhistorische Bestandsanalyse und Denkmalschutz (Architekten und Bauunternehmen)

Bei der Sanierung historischer Altbauten in Deutschland fehlen oft zuverlässige Pläne über die Lage tragender Wände, Kamine oder verdeckter Fachwerkstrukturen. Ein Architekt begeht das Gebäude mit dem Honeywell CT45 XP, das mit einem hochauflösenden mmWave-Wandscanner gekoppelt ist. Während der Architekt durch die Räume geht, erstellt das visuelle SLAM-System ein präzises Oberflächen-Mesh der Räumlichkeiten. Gleichzeitig scannt das mmWave-Radar das Mauerwerk. Durch die unterschiedlichen dielektrischen Eigenschaften von Ziegeln, Holzdecken and barocken Hohlwandkonstruktionen erkennt der Tiny Agent verdeckte Strukturen und Holzbalken hinter dem Putz.

Da historische Gebäude oft stark verwinkelt sind, verhindert der lokale Kontextspeicher der Applikation ein Auseinanderdriften der Raumkoordinaten bei wiederholten Begehungen (Loop Closure). Das System erkennt bereits kartierte Räume wieder und optimiert das gesamte Raummodell rückwirkend. Das resultierende 3D-Modell, welches die sichtbare Raumgeometrie mit den verdeckten Tragstrukturen vereint, wird via OPC UA direkt in das CAD-System des Architekten exportiert und als digitaler Zwilling in 3dxStage zur statischen Beurteilung bereitgestellt, wodurch zerstörerische Probebohrungen vermieden werden.

### Szenario 4: Temporäre Stadtplanung und Sicherheitszonen-Management (Stadtplanungsämter)

Für Großveranstaltungen im urbanen Raum, wie Weihnachtsmärkte oder temporäre Fanmeilen, müssen temporäre Absperrungen, Fluchtgassen und Überwachungsbereiche kurzfristig geplant und überprüft werden. Mitarbeiter des Stadtplanungsamtes begehen das Areal mit dem CT45 XP. Das Gerät erfasst die temporär errichteten Zäune, Bühnenstrukturen und Sicherheitsbarrieren im Vorbeigehen. Über den persistenten Speicher des Geräts werden diese Daten lokal gecacht, falls im dicht gedrängten urbanen Raum das Mobilfunknetz überlastet ist.

Der Tiny Agent gleicht die neu erfassten Barrieren kontinuierlich mit dem statischen Geoportal-Modell der Stadt ab. Über die integrierten Netzwerkschnittstellen des CT45 XP werden die aktualisierten Daten via MQTT an die Leitstelle übermittelt. In der 3dxStage-Umgebung der Stadtplanung wird das aktuelle 3D-Modell der Veranstaltung live gerendert. Planer können sofort simulieren, ob die Fluchtwegbreiten den gesetzlichen Vorgaben entsprechen, ob ausreichende Sichtachsen für Rettungskräfte vorhanden sind und ob temporäre Verkaufsstände die Hydranten blockieren.

### Szenario 5: Crowd-Simulation und Sicherheitsforschung (Universitäten und Forschungseinrichtungen)

Im Rahmen von Forschungsprojekten zur Analyse von Massenpaniken und Personenströmen in Transferbereichen (z. B. Bahnhöfen oder Flughäfen) betreiben Universitäten Testbereiche, die mit verteilten Sensor-Arrays ausgestattet sind. Testpersonen tragen das Honeywell CT45 XP am Körper, welches kontinuierlich BLE-Signale der Umgebungsscanner empfängt und seine eigenen IMU-Daten aufzeichnet. Der Tiny Agent auf dem Gerät berechnet daraus ein hochpräzises individuelles Bewegungsprofil.

Der persistente Speicher auf dem Gerät fungiert hierbei als "Verhaltens-Logbuch", das die Trajektorie der Testperson über mehrere Stunden hinweg lückenlos und datenschutzkonform (vollständig anonymisiert auf dem Endgerät) aufzeichnet. Die Daten werden periodisch über eine OPC UA Pub/Sub-Schnittstelle in ein zentrales Crowd-Simulationssystem eingespeist. In der wissenschaftlichen 3dxStage-Plattform werden diese realen Bewegungsdaten mit synthetischen Crowd-Modellen fusioniert. Dies ermöglicht es Forschern, menschliche Interaktionsradien, Reaktionszeiten bei plötzlichen Hindernissen und kollektive Bewegungsmuster mathematisch exakt zu modellieren, um neue Sicherheitsstandards für zukünftige Infrastrukturprojekte in Deutschland zu definieren.

---

<a id="6-fazit-und-strategische-empfehlungen"></a>
## 6. Fazit und strategische Empfehlungen

Die Etablierung eines mobilen 3D-Erfassungs- und Wanddurchdringungssystems auf Basis des Honeywell CT45 XP stellt eine technologisch anspruchsvolle, aber hochgradig realisierbare Lösung für den professionellen Einsatz in Deutschland dar. Durch die gezielte Fusion lokaler Inertialsensorik mit externen, hochfrequenten Radarsystemen lassen sich die physikalischen Grenzen optischer Erfassungsmethoden erfolgreich überwinden.

Für eine erfolgreiche Systemintegration und den rechtskonformen Betrieb im industriellen und behördlichen Sektor werden folgende strategische Maßnahmen empfohlen:

- **Frequenztechnische Absicherung:** Für den Betrieb der mmWave- und UWB-Sensorkomponenten im Außenbereich ist eine strikte Einhaltung der BNetzA-Grenzwerte für die spektrale Leistungsdichte zwingend erforderlich. Bei großflächigen Industrie- oder Forschungsarealen sollte die Zuteilung einer privaten Campuslizenz im 26-GHz-Band beantragt werden, um exklusive, interferenzfreie Übertragungskanäle für die 3D-Datenströme zu sichern.
- **Implementierung einer resilienten Edge-Architektur:** Die mobile Software-Applikation auf dem Honeywell CT45 XP muss konsequent nach dem "Offline-First"-Prinzip konzipiert werden. Die Nutzung des SQLite-WAL-Caches garantiert eine lückenlose Datenpersistenz und schützt das System vor Datenverlusten bei abrupten Verbindungsabbrüchen im dichten Gebäudeinneren oder während eines warmen Akkuwechsels.
- **Standardisierte Datenintegration:** Zur Anbindung des mobilen Erfassungssystems an übergeordnete Visualisierungsplattformen wie 3dxStage ist die Implementierung einer OPC UA-zu-MQTT-Schnittstelle unter Nutzung des Sparkplug B-Standards zu priorisieren. Dies gewährleistet eine semantisch reichhaltige, herstellerunabhängige und bandbreiteneffiziente Datenübertragung, die den hohen IT-Sicherheitsanforderungen kritischer Infrastrukturen nach NIS-2 entspricht.
