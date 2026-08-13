#!/usr/bin/env python3
"""Static check: USB ids in the manifest filter, the Kotlin constants and the docs agree.

Android's `<usb-device>` filter takes **decimal** vendor/product ids, while
every datasheet, driver and forum post quotes **hex**. That conversion is done
by hand, and when it is wrong nothing crashes — the sensor is simply never
detected, which is the failure mode hardest to diagnose in the field.

This checks three things:

1. Every `0xVVVV / 0xPPPP` written in a comment matches the decimal attributes
   on the line below it.
2. Every id is a known entry from the USB-IF registry (the table below was
   verified against usb-ids.gowdy.us on 2026-08-13).
3. Every vendor id referenced from Kotlin appears in the manifest filter, so a
   device the code looks for is also one Android will notify us about.

Run:  tools/check-usb-ids.py
Exit: 0 clean, 1 problems found.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

FILTER = Path("android-app/app/src/main/res/xml/usb_device_filter.xml")
TRANSPORT = Path("android-app/app/src/main/java/com/aura/agent/sensors/UsbSerialTransport.kt")

# Verified against the USB-IF registry (usb-ids.gowdy.us), 2026-08-13.
REGISTRY: dict[tuple[int, int], str] = {
    (0x10C4, 0xEA60): "Silicon Labs CP210x UART Bridge",
    (0x10C4, 0xEA70): "Silicon Labs CP2105 Dual UART Bridge",
    (0x0403, 0x6001): "FTDI FT232 Serial (UART) IC",
    (0x0403, 0x6010): "FTDI FT2232C/D/H Dual UART/FIFO IC",
    (0x0451, 0xBEF3): "TI XDS110 (CC1352R1 Launchpad)",
    (0x0BDA, 0x2838): "Realtek RTL2838 DVB-T",
    (0x0BDA, 0x2832): "Realtek RTL2832U DVB-T",
}

VENDOR_NAMES = {
    0x10C4: "Silicon Labs",
    0x0403: "FTDI",
    0x0451: "Texas Instruments",
    0x0BDA: "Realtek",
}


def main() -> int:
    if not FILTER.is_file():
        print(f"error: {FILTER} not found", file=sys.stderr)
        return 2

    xml = FILTER.read_text(encoding="utf-8")
    problems: list[str] = []

    # ---------------------------------------------- 1. declared ids are real
    pairs = [(int(v), int(p)) for v, p in
             re.findall(r'vendor-id="(\d+)"\s+product-id="(\d+)"', xml)]
    if not pairs:
        problems.append("no <usb-device> entries found")

    for vid, pid in pairs:
        if (vid, pid) not in REGISTRY:
            problems.append(
                f"0x{vid:04X}/0x{pid:04X} ({vid}/{pid}) is not a known registry entry"
            )

    # ------------------------------------- 2. hex comments match the decimals
    for m in re.finditer(
        r"0x([0-9A-Fa-f]{4})\s*/\s*0x([0-9A-Fa-f]{4})[^>]*-->\s*\n\s*"
        r'<usb-device vendor-id="(\d+)" product-id="(\d+)"',
        xml,
    ):
        hv, hp = int(m.group(1), 16), int(m.group(2), 16)
        dv, dp = int(m.group(3)), int(m.group(4))
        if (hv, hp) != (dv, dp):
            problems.append(
                f"comment says 0x{hv:04X}/0x{hp:04X} but the filter declares "
                f"{dv}/{dp} (= 0x{dv:04X}/0x{dp:04X})"
            )

    # --------------------------- 3. Kotlin vendor ids appear in the filter too
    if TRANSPORT.is_file():
        kt = TRANSPORT.read_text(encoding="utf-8")
        declared_vids = {v for v, _ in pairs}
        declared_pairs = set(pairs)
        for name, hexval in re.findall(
            r"const val (VID_[A-Z_]+)\s*=\s*0x([0-9A-Fa-f]+)", kt
        ):
            vid = int(hexval, 16)
            if vid not in declared_vids:
                problems.append(
                    f"{name} = 0x{vid:04X} is used by the code but no "
                    f"<usb-device> entry declares it; Android will not notify "
                    f"the app when that device is attached"
                )

        # Each PID constant the code names must also be declared. A vendor-id
        # match is not enough: dropping the CP2105 entry while keeping the
        # CP2102 one leaves 0x10C4 present, so a vendor-only check passes
        # while Rev C/D mmWave boards go undetected.
        for name, hexval in re.findall(
            r"const val (PID_[A-Z0-9_]+)\s*=\s*0x([0-9A-Fa-f]+)", kt
        ):
            pid = int(hexval, 16)
            if not any(p == pid for _, p in declared_pairs):
                problems.append(
                    f"{name} = 0x{pid:04X} is named in the code but no "
                    f"<usb-device> entry declares that product id"
                )

    if problems:
        print("USB id check FAILED:")
        for p in problems:
            print(f"  - {p}")
        return 1

    print(f"OK: {len(pairs)} USB ids, all registry-valid and consistent.")
    for vid, pid in pairs:
        print(f"   0x{vid:04X}/0x{pid:04X}  {REGISTRY[(vid, pid)]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
