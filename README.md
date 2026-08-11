# dang88bang-pixel-aura-mesh

Interaktive, gerenderte Dokumentation zum Thema:

> **Hochauflösende 3D-Raumerfassung und Wanddurchdringung mittels Honeywell CT45 XP**
> *Systemarchitektur, mathematische Sensorfusion und regulatorische Praxis in Deutschland*

## Dokumentation

Der Doku-Hub (`index.html`) lädt die folgenden Markdown-Quelldateien und rendert sie im Browser mit **MathJax** (Formeln) und **marked** (Markdown), jeweils mit Inhaltsverzeichnis:

| Datei | Inhalt |
| --- | --- |
| `docs/whitepaper.md` | Whitepaper: Systemarchitektur, 5 Realisierungsansätze, EKF-Sensorfusion, Dämpfungstabellen, regulatorische Rahmenbedingungen (BNetzA/BSI/NIS-2), 5 Anwendungsszenarien, Fazit |
| `docs/app-projektvorlage.md` | Vollständige Android-App-Projektvorlage (Kotlin, Jetpack Compose, Clean Architecture, EKF, BLE-Triangulation, Room, MQTT/Sparkplug B, Sceneform-3D-Ansicht) inkl. `build.gradle` und CT45-XP-Hinweisen; Kap. 11 (Swarm Mapping / Collaborative SLAM) und Kap. 12 (Bedienkonzept) |
| `docs/swarmradar.md` | Eigenständiges, produktionsreifes App-Projekt „SwarmRadar": Clean Architecture + MVVM, Collaborative EKF, Punktwolken-Fusion/Voxel-Filter, MQTT/Sparkplug B, Room WAL + Dual-Write, Energie-/Thermomanagement, Recherche-Integration (wallhacks./NorthWorks/ViSAR/HoloRadar); Kap. 10 „Cognitive SwarmRadar" (prädiktive LSTM-Intelligenz, autonome Entscheidung, Anti-Jamming, Drohnen/TETRA, Non-Visual-Bedienung, GNSS-Pose-Graph, Self-Healing) |

## Dokumentation lokal ansehen

Die Vorschau-Seite lädt die Markdown-Dateien per `fetch()`, daher wird ein kleiner
HTTP-Server benötigt (kein `file://`-Zugriff):

```bash
python3 -m http.server 8000
# dann http://localhost:8000/ öffnen (Dokumentwechsel über ?doc=app bzw. ?doc=whitepaper)
```

## Kurzüberblick (Whitepaper)

| Realisierungsmethode | Physikalische Durchdringung | Typ. Genauigkeit | Eignung dynam. Objekte |
| --- | --- | --- | --- |
| ① BLE-Triangulation + IMU | Nein | 1,0–3,0 m | Eingeschränkt |
| ② Vision- + IMU-SLAM | Nein (Sichtlinie) | 0,05–0,2 m | Gut (Sichtbereich) |
| ③ Externe Sensor-Fusion | **Ja** | 0,1–0,3 m | **Hervorragend** |
| ④ KI-basierte Hypothesen | Virtuell | Statistisch | Befriedigend |
| ⑤ Hybrid-BIM-Mapping | Virtuell | 0,5–1,5 m | Gut (IoT-Overlay) |
