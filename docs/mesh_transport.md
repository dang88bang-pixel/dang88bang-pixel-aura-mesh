# Mesh transport — fitting AURA into a LoRa packet

The proposals recommend Meshtastic as the netzunabhängig transport for AURA's
CoT data and rate the effort *"gering bis mittel — CoT-Konverter in wenigen
Tagen"*. The direction is right and it is now implemented. The effort rating
is not, and the reason is worth stating precisely, because it changes what the
feature can do rather than how long it takes.

## The two constraints the proposals omit

### 1. CoT XML does not fit

Measured against the running agent:

| Payload | Size |
| --- | --- |
| One CoT `self` event | **470 bytes** |
| Meshtastic maximum payload | 237 bytes |
| Meshtastic practical payload | ~200 bytes (deliverability collapses above) |

A single position report is **2.4× over budget** before a single contact is
added. `self + 3 contacts` is ~1 822 bytes — nine packets for one frame.

The proposal's own text says LoRa is "ausreichend für Text, Positionsdaten,
kurze Binärnachrichten" and simultaneously proposes sending CoT-XML over it.
Those two statements are incompatible; CoT-XML is not a short binary message.

### 2. Duty cycle, not bandwidth, is the binding constraint

EN 300 220 caps EU 868 MHz at **1 %**. At roughly 1.4 s of airtime for a
200-byte LongFast packet:

```
required silence = 1.4 s / 0.01 = 140 s per packet
                 → ~26 packets per hour, per sender, legally
```

AURA's fusion loop runs at **10 Hz**. The gap between what the pipeline
produces and what the radio may lawfully emit is about five orders of
magnitude. So the interesting engineering is not the converter — it is
**deciding what not to send**. No proposal mentions duty cycle at all.

## What was built

`aura/mesh.py` plus `GET /api/v1/agent/export/mesh`.

### A compact binary frame

8-byte header + 22 bytes per record:

| Field | Bytes | Notes |
| --- | --- | --- |
| `uid_hash` | 4 | CRC32 of the CoT UID — stable, lets a receiver correlate tracks without carrying UID strings |
| `lat_e7`, `lon_e7` | 4 + 4 | int32 × 1e7 ≈ 1.1 cm, two orders finer than the `ce` we report |
| `alt` | 2 | metres |
| `ce`, `le` | 2 + 2 | decimetres, saturating at 6553.5 m |
| `kind`, `quality`, `confidence`, `flags` | 1 each | self/contact, the four quality tiers, 0–255, behind-wall bit |

Measured against the live agent:

| Contacts | CoT XML | Binary | Ratio | Fits? |
| --- | --- | --- | --- | --- |
| 0 | 470 B | **30 B** | 15.7× | yes |
| 1 | 932 B | 52 B | 17.9× | yes |
| 3 | 1 822 B | 96 B | 19.0× | yes |
| 7 | 3 602 B | 184 B | 19.6× | yes |
| 15 | 7 172 B | 184 B (truncated) | 39.0× | yes |

Truncation is deliberate and ordered — **self is always first**, because a
frame the radio silently drops is worse than a frame missing a contact.

### An explicit duty-cycle accountant

`SendBudget` refuses to transmit for two independent reasons, and returns the
reason as text rather than dropping traffic silently:

- **Regulatory** — 140 s of owed silence after each packet. Exceeding it is
  not a performance question, it is unlawful.
- **Pointless traffic** — a stationary operator re-sending an identical
  position spends budget a moving contact will need. Gate: 5 m of movement.
- **Heartbeat** — but after 900 s it sends anyway, because silence must not be
  ambiguous between "not moving" and "dead".

## What this is not

**It will not interoperate with the Meshtastic ATAK plugin.** That plugin uses
`TAKPacket` protobuf over portnum 72/78 with **zstd dictionary compression**,
reaching 754 B XML → 98 B (87 %). That is the better answer where you can use
it, and `meshtastic/TAKPacket-SDK` exists precisely to do this correctly across
platforms.

We did not use it because `protobuf` and `zstandard` are not dependencies of
this project, and adding a compiled protobuf toolchain plus a pre-trained zstd
dictionary — to produce a wire format we cannot test against real hardware —
would be false precision. This codec is **AURA's own**, in the same sense as
[`uwb_anchor_protocol.md`](uwb_anchor_protocol.md): exactly specified,
round-trip tested, and honest that the receiving end must be our decoder.

**If interoperability with the official plugin becomes the requirement, adopt
TAKPacket-SDK — do not extend this.** That is a real decision point, not a
detail; it is recorded here so nobody later mistakes this format for the
standard one.

Also untested against hardware: no LoRa module, no Meshtastic node. The 1.4 s
airtime figure is a documented approximation for LongFast, not a measurement
from our own radio. Everything above the physical layer is tested; the
physical layer is not.

## Verification

```
$ curl -s localhost:8080/api/v1/agent/export/mesh -D -
HTTP 200
x-aura-records: 1
x-aura-sent: 1
x-aura-frame-bytes: 30

decoded: 1 record, kind=1 lat=52.375960 lon=9.732092 ce=0.10 q=good
XML 470 B -> binary 30 B = 15.7x
fits one LoRa packet: YES
```

22 tests in `tests/test_mesh.py`. The first one asserts that CoT XML *exceeds*
the LoRa budget — if that ever stops being true, this module's justification
has changed and the test says so.

## Recommendation

1. **Done: the codec and the budget.** Both constraints are now explicit in
   code rather than discovered in the field.
2. **Next, and it is hardware work:** a LoRa module on USB-OTG, a real
   airtime measurement to replace the 1.4 s approximation, and a decision on
   whether interoperability with the official plugin is required.
3. **Do not** plan on streaming point clouds or 3D models over LoRa. At ~26
   packets/hour/sender the medium carries positions and alerts, nothing more —
   which the proposal does state correctly.

*Verified 2026-08-13 against the running agent, Meshtastic payload
documentation (237 B max / ~200 B practical), EN 300 220 duty-cycle limits,
and the Meshtastic ATAK plugin's TAKPacket/zstd design.*
