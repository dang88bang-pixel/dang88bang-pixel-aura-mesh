# Aura anchor protocol (UWB)

**This is our protocol, not Qorvo's.** It will not work with stock DWM3001CDK
firmware. If you buy an evaluation board and flash the shipped CLI/UCI
application, none of the commands below exist.

That needs saying plainly, because the code used to describe this as "the
DWM3000 shell format", which reads like a vendor specification and would lead
someone to buy hardware expecting it to work out of the box.

## What the vendor actually provides

Checked against Qorvo's documentation and support forum on 2026-08-13:

- The DWM3001CDK exposes a **USB CDC virtual COM port** on the user USB
  connector (J20). J9 is the J-Link port and is for flashing and debugging
  only — you cannot talk to the application through it.
- The shipped firmware offers a **CLI** or a **UCI** application. Their command
  sets are defined by that firmware, not by a published wire standard.
- A `UART <0|1>` command redirects the console from USB CDC to the raw UART on
  the Raspberry Pi header, which is the path to use when a second
  microcontroller drives the board.
- There is no `$RANGE` command, and no vendor-published ASCII ranging format.

The DW3000 API itself is a C driver (`dwt_*`), not a serial protocol. Any text
protocol over a serial link is necessarily something the integrator defines.

## The protocol this project expects

The anchor firmware must implement the following over a serial link at
**115200 8N1**.

### Commands (host to anchor)

| command | meaning |
|---|---|
| `$INIT\r\n` | initialise the radio and begin a ranging session |
| `$RANGE\r\n` | request one ranging round against all configured anchors |
| `$STOP\r\n` | end the session and stop transmitting |

### Reply (anchor to host)

One line per ranging round, `\r\n` terminated:

```
ANCHOR-A=3.214,ANCHOR-B=7.882;CIR=0.42,1.87
```

- Before the `;` — comma-separated `<anchor-id>=<range-in-metres>` pairs. The
  id is free-form text without `,`, `=` or `;`.
- A trailing `*` on a range marks that link **non-line-of-sight**:
  `ANCHOR-A=3.2*`. NLOS links are kept and their sigma inflated, not dropped —
  with only one or two clean anchors the geometry is underconstrained.
- After the `;` — optionally `CIR=<amplitude>,<phase>`, the channel impulse
  response of the tracked range bin. This is what the vitals estimator
  consumes; respiration and heartbeat are not visible in the *range*, only in
  the phase modulation of a static multipath tap.

A line carrying neither a range nor a CIR sample is ignored. Malformed tokens
are skipped rather than failing the line, because serial framing errors put
garbage on the wire regularly and one bad line must not stop the reader.

Parsing is implemented once per platform and tested against the same cases:

- `edge-agent/aura/sensors/uwb.py`
- `android-app/.../sensors/UwbGeometry.kt` (55 host-side checks)

## Alternatives, and why they were not chosen

| option | cost |
|---|---|
| **Custom anchor firmware** (chosen) | firmware must be written and maintained; total control over the CIR output, which the stock CLI does not expose at all |
| Speak UCI to stock firmware | no firmware work, but UCI is a binary session-based protocol — considerably more client code — and it still does not surface the raw CIR that through-wall sensing depends on |
| Use the platform UWB API | requires API 31+ and a device with UWB hardware; the CT45P-X0N is Android 11 with no UWB radio, so this cannot be the primary path |

The deciding factor is the CIR. Ranging alone is available several ways; the
channel impulse response is what makes respiration detection possible, and
exposing it requires firmware we control.

## Status

**Unverified against hardware.** No DWM3000 board has been connected to this
code. The parsers are tested against the format above, and the format is
internally consistent across both implementations, but whether real firmware
produces it is exactly the thing that cannot be checked here. See
`docs/source_claims.md`.
