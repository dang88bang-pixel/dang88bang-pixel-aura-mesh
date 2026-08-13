# AURA 6.0 — Benutzerhandbuch

> Kurzfassung für Einsatzkräfte, Architekten und Forschende.
> Technische Details: `architecture.md` · Grenzwerte: `performance_targets.md`

---

## 1. Was das System kann — und was nicht

**Kann:**
- Räume während der Begehung als 3D-Punktwolke und Grundriss aufnehmen
- Die eigene Position ohne GPS auf ca. **15 cm** genau halten (mit UWB-Ankern)
- Personen hinter Wänden über Atembewegung detektieren (UWB-Mikro-Doppler)
- Evakuierungen simulieren inklusive Rauchausbreitung und Engstellen
- Alles manipulationssicher protokollieren (SHA-256-Hashkette)

**Kann nicht:**
- Personen hinter Wänden **identifizieren** — nur „da atmet jemand"
- Durch Stahlbeton mit dichter Bewehrung zuverlässig messen
- Ohne UWB-Anker oder BLE-Token die Karte georeferenzieren
- Passivradar mit einem RTL-SDR feiner als ca. **62 m** auflösen

---

## 2. Schnellstart

### 2.1 Edge-Agent starten

```bash
cd edge-agent
pip install -r requirements.txt
python agent.py                      # Simulation, Port 8080
```

Mit echter Hardware:
```bash
AURA_SIMULATE=0 \
AURA_LIDAR_PORT=/dev/ttyUSB0 \
AURA_MMWAVE_PORT=/dev/ttyACM0 \
python agent.py --hardware
```

Docker:
```bash
cd edge-agent && docker compose up -d
```

### 2.2 Visualisierung starten

```bash
cd web-visualizer
npm install
npm start                            # http://localhost:3000
```

Läuft kein Agent, zeigt die Oberfläche automatisch eine **simulierte Begehung**
(gelbes Banner „Demo-Modus"). So lässt sich die Bedienung ohne Hardware üben.

### 2.3 Android-App bauen

```bash
cd android-app
./gradlew assembleDebug
adb install app/build/outputs/apk/debug/app-debug.apk
```

---

## 3. Die Weboberfläche

```
┌──────────────────────────────────────────────────────────────┐
│ AURA 6.0                                    60 FPS  VERBUNDEN │
├───────────────┬──────────────────────────┬───────────────────┤
│ Szenario      │                          │ EKF-Zustand   FIX │
│  Typ          │                          │  Position         │
│  Personen     │      3D-Ansicht          │  Unsicherheit     │
│  Rauchdichte  │   (Maus: drehen,         │  Kovarianz-Chart  │
│  Panikfaktor  │    Rad: zoomen,          │                   │
│  [Start][Stop]│    rechts: schwenken)    │ Detektionen       │
│               │                          │  Personen         │
│ Ebenen        │                          │  Durch Wand       │
│  ☑ Punktwolke │                          │  Atmung / Puls    │
│  ☑ Wände      │  ┌────────────────────┐  │                   │
│  ☑ Personen   │  │ Legende            │  │ BLE-Token         │
│  ☑ Durch-Wand │  └────────────────────┘  │  ▓▓▓▓▓░░ -62 dBm  │
│  ☑ BLE-Token  │                          │                   │
│  ☑ Rauch      │                          │ Gerät             │
│               │                          │  Akku · Temp      │
│ Zeitachse     │                          │                   │
│  ├──────●──── │                          │ Export            │
│               │                          │  [PNG][glTF][JSON]│
└───────────────┴──────────────────────────┴───────────────────┘
```

### 3.1 Farbcode (überall identisch)

| Farbe | Bedeutung |
|---|---|
| **Grau** `#6A7A8A` | Struktur: Wände, Möbel |
| **Signalgrün** `#00FF88` | Lebewesen |
| **Bernstein** `#FFCC00` | Geräte: BLE-Token, Access Points |
| **Rot** `#FF4444` | Gefahrenzone / Durch-Wand-Detektion |
| **Orange** `#FF8800` | Ausgänge und Fluchtwege |
| **Blau** `#2E7DD1` | Eigene Position und Laufweg |

### 3.2 Bedienung

| Aktion | Ergebnis |
|---|---|
| Linke Maustaste ziehen | Szene drehen |
| Mausrad | Zoomen |
| Rechte Maustaste ziehen | Schwenken |
| „Draufsicht" | Grundriss-Perspektive |
| „Position folgen" | Kamera folgt dem Träger |
| Ebenen-Häkchen | Kategorie ein-/ausblenden |
| Zeitachse | in der Historie zurückspringen |
| PNG / glTF / JSON | Screenshot bzw. 3D-Modell bzw. Rohdaten |

---

## 4. Die Android-App

### 4.1 Tabs

**Live** — Lagekugel (künstlicher Horizont), LiDAR-Sweep von oben,
RSSI-Balken je Token, Vitalzeichen, Akku und Temperatur.

**Karte** — dieselbe 3D-Ansicht wie im Browser, in einer WebView.

**Szenarien** — Typ wählen, Personenzahl / Rauchdichte / Panik einstellen,
starten. Der Fortschrittsbalken zeigt die simulierte Zeit.

**Einstellungen** — Server-URL, mmWave-Modus, BLE-Token verwalten,
Speicherbelegung, **Audit-Log prüfen**.

### 4.2 Erste Begehung

1. Anker setzen (optional, aber empfohlen): 3–4 UWB-Anker an bekannten
   Positionen. Ohne sie driftet die Karte langsam.
2. BLE-Token verteilen und in den Einstellungen mit Position anlegen.
3. **Scan starten**. Ruhig gehen, ca. 0,8 m/s.
4. An Türen kurz stehenbleiben — das erzeugt eine Nullgeschwindigkeits-
   Stützung und reduziert die Drift.
5. Räume vollständig umrunden; Ecken sind wichtiger als Flächen.
6. **Karte speichern**. Die Version wird mit Zeitstempel abgelegt.

### 4.3 Warum stehenbleiben hilft

Der Filter erkennt Stillstand über ein Fenster von 12 IMU-Samples und setzt
dann die Geschwindigkeit auf null. Das ist der wirksamste Einzelmechanismus
gegen Drift. Ein einzelnes Sample genügt **nicht** — beim Gehen sieht die
Standphase jedes Schritts wie Stillstand aus.

---

## 5. Szenarien

### 5.1 Evakuierung
Personenzahl, Rauchdichte und Panikfaktor einstellen, Brandherd optional
setzen. Ergebnis: Räumungszeit (Median / 95 %), Engstellen, Rauchdosis.

*Interpretation:* Die Engstellen sind das eigentliche Resultat. Absolute
Räumungszeiten hängen stark von den angenommenen Gehgeschwindigkeiten ab.

### 5.2 Taktik
Blaue Marker = eigene Kräfte, rote Halos = Durch-Wand-Detektionen.
Gefahrenzonen lassen sich in der Karte markieren.

### 5.3 Architektur / Bestandsaufnahme
Nach der Begehung `glTF` exportieren → Import in Blender, Revit (via
Konverter), Rhino oder jede BIM-Software.

*Genauigkeit:* Wandlagen ca. ±5 cm bei sauberer Begehung mit Ankern.
Für Bestandspläne ausreichend, für Fertigungstoleranzen nicht.

### 5.4 Veranstaltung
Mehrere CT45P laden ihre RSSI-Daten über `/api/v1/agent/ingest` hoch; der
Agent fusioniert sie zu einem Gesamtbild der Personenströme.

### 5.5 Forschung
Jeder Scan wird als Version gespeichert. Über die Zeitachse lassen sich
Zustände vergleichen — z. B. Baufortschritt über Wochen.

---

## 6. Durch-Wand-Detektion verstehen

Die Erkennung beruht auf **Atembewegung**, nicht auf einem Bild:

1. UWB sendet Impulse; die Kanalimpulsantwort wird abgetastet.
2. Die Brustwand moduliert die Phase eines Multipath-Pfades um Bruchteile
   eines Millimeters.
3. Eine FFT über ~13 s zeigt einen Peak bei 0,12–0,65 Hz (7–39 Atemzüge/min).
4. Ein Peak über **18 dB SNR** in **drei aufeinanderfolgenden Fenstern**
   gilt als Detektion.

**Warum so streng?** Bei 6 dB lag die Falsch-Positiv-Rate auf reinem Rauschen
bei **33 %** — das System hätte in leeren Gebäuden Personen gemeldet. Echte
Atmung liefert 33+ dB, Rauschen maximal ~10 dB.

**Grenzen:**
- Reichweite realistisch 3–6 m durch eine Trockenbau- oder Ziegelwand
- Stahlbeton mit dichter Bewehrung: meist kein Durchdringen
- Eine sich schnell bewegende Person erzeugt **kein** Atemsignal —
  dafür ist mmWave zuständig
- Mehrere Personen dicht beieinander erscheinen als eine Detektion

---

## 7. Audit-Log

Jede sicherheitsrelevante Aktion wird in eine Hashkette geschrieben. Prüfung
in den Einstellungen oder per API:

```bash
curl http://localhost:8080/api/v1/agent/audit/verify
```

```json
{ "valid": true, "message": "chain of 142 entries is intact" }
```

Wurde manipuliert, nennt die Antwort den **Index des ersten veränderten
Eintrags**. Das ist vor Gericht der Unterschied zwischen „irgendetwas stimmt
nicht" und „Eintrag 87 wurde nachträglich geändert".

---

## 8. Fehlerbehebung

| Symptom | Ursache | Abhilfe |
|---|---|---|
| „Position: unsicher" bleibt | zu wenige UWB-Anker in Sicht | mindestens 3 Anker, besser 4 |
| Karte driftet / doppelte Wände | zu schnell gegangen | ca. 0,8 m/s, an Türen kurz halten |
| Keine BLE-Token sichtbar | Standortberechtigung fehlt | Android verlangt Fine Location für BLE-Scan |
| Visualisierung schwarz | WebGL blockiert | Hardwarebeschleunigung im Browser aktivieren |
| „Demo-Modus"-Banner | Agent nicht erreichbar | `AGENT_URL` prüfen, `curl :8080/health` |
| Durch-Wand meldet nichts | Wand zu dick / Person bewegt sich | mmWave-Ansicht prüfen |
| Proxy meldet 503 | Agent gestoppt | Agent-Log prüfen |

---

## 9. Rechtliche Hinweise (DE/EU)

- **UWB** ist auf 6–8,5 GHz bei −41,3 dBm/MHz EIRP begrenzt (ETSI EN 302 065).
  Höhere Leistung für mehr Reichweite braucht eine BNetzA-Zuteilung.
- **Vitaldaten** (Atmung, Puls) sind personenbezogene Gesundheitsdaten nach
  DSGVO Art. 9, sobald sie einer Person zuordenbar sind → DSFA erforderlich.
- **Am Arbeitsplatz** ist der Betriebsrat nach BetrVG §87 Abs. 1 Nr. 6
  **vor** der Installation zu beteiligen.
- **BOS-Einsatz** zur Menschenrettung hat eine deutlich breitere Rechtsgrund-
  lage als Übung oder Bestandsaufnahme. Das Audit-Log dient dem Nachweis.

Keine Rechtsberatung — vor dem Einsatz juristisch prüfen lassen.
