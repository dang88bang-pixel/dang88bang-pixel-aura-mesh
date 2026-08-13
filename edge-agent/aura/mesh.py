"""Mesh transport codec: fitting AURA telemetry into a LoRa packet.

The proposals describe Meshtastic as a drop-in transport for CoT and rate the
effort "gering". Measured against the actual constraints, that is wrong in two
specific ways, and both are load-bearing:

**1. CoT XML does not fit.** One `self` event from ``aura.cot`` is **470
bytes**. Meshtastic's maximum payload is 237 bytes and the practical limit is
~200 before deliverability collapses. So a single position report is already
2.4x over budget, and ``self + 3 contacts`` is ~1777 bytes — nine packets for
one frame. You cannot put CoT XML on LoRa unmodified.

**2. Duty cycle, not bandwidth, is the binding constraint.** EN 300 220 caps
EU 868 MHz at **1%**. A ~200-byte LongFast packet is roughly 1.4 s of airtime,
which obliges ~140 s of silence after it: about **26 packets per hour, per
sender, legally**. AURA's fusion loop runs at 10 Hz. The gap between what the
pipeline produces and what the radio may legally emit is ~5 orders of
magnitude, so the interesting engineering is *deciding what not to send*.

This module therefore does two things:

* :class:`MeshCodec` — a compact binary frame. 8-byte header + 22 bytes per
  contact, so ``self + 7 contacts`` is 184 bytes and fits one packet.
* :class:`SendBudget` — an explicit duty-cycle accountant that refuses to
  emit when the budget is spent, and a change filter so a stationary operator
  does not burn the budget re-sending the same position.

How this relates to the official plugin
---------------------------------------
Meshtastic's own ATAK plugin solves the size problem with `TAKPacket`
protobuf + zstd dictionary compression, reaching 754 B XML -> 98 B (87%). That
is the better answer where you can use it. We do not: `protobuf` and
`zstandard` are not dependencies of this project, and adding a compiled
protobuf toolchain plus a pre-trained zstd dictionary to reach a wire format
we cannot test against real hardware would be false precision.

So this codec is **AURA's own**, in the same sense as
``docs/uwb_anchor_protocol.md``: it will not interoperate with the Meshtastic
ATAK plugin, and the receiving end must be our own decoder. It is deliberately
simple, exactly specified, and round-trip tested. If interoperability with the
official plugin becomes the requirement, the right move is adopting
TAKPacket-SDK, not extending this.
"""

from __future__ import annotations

import math
import struct
import time
import zlib
from dataclasses import dataclass, field
from typing import Any, Iterable

# --- wire constraints (measured / regulatory, not invented) -----------
MESHTASTIC_MAX_PAYLOAD = 237      # hard protocol limit, bytes
MESHTASTIC_SAFE_PAYLOAD = 200     # deliverability degrades sharply beyond this
EU868_DUTY_CYCLE = 0.01           # EN 300 220, 868.0-868.6 MHz
LONGFAST_AIRTIME_200B = 1.4       # seconds, approximate, for budgeting

FRAME_MAGIC = 0xA6                # 'AURA6'
FRAME_VERSION = 1

_HEADER = struct.Struct("<BBIH")   # magic, version, epoch_s, flags
_RECORD = struct.Struct("<IiihHHBBBB")
# uid_hash, lat_e7, lon_e7, alt_m, ce_dm, le_dm, kind, quality, confidence, flags

KIND_SELF = 1
KIND_CONTACT = 2

QUALITY_CODES = {"good": 0, "degraded": 1, "poor": 2, "lost": 3}
QUALITY_NAMES = {v: k for k, v in QUALITY_CODES.items()}

FLAG_BEHIND_WALL = 0x01


def _uid_hash(uid: str) -> int:
    """Stable 32-bit id. CRC32 is not a security primitive and is not used as
    one -- it only has to be stable and cheap, so a receiver can correlate
    tracks across frames without carrying full UID strings on the wire."""
    return zlib.crc32(uid.encode("utf-8")) & 0xFFFFFFFF


def _clamp(value: float, low: float, high: float) -> float:
    if not math.isfinite(value):
        return low
    return max(low, min(high, value))


@dataclass
class MeshRecord:
    """One position on the wire: 22 bytes."""

    uid: str
    lat: float
    lon: float
    alt: float = 0.0
    ce: float = 0.0
    le: float = 0.0
    kind: int = KIND_CONTACT
    quality: str = "lost"
    confidence: float = 0.0
    behind_wall: bool = False

    def pack(self) -> bytes:
        # lat/lon as int32 * 1e7: ~1.1 cm resolution, far finer than our ce.
        lat_e7 = int(round(_clamp(self.lat, -90.0, 90.0) * 1e7))
        lon_e7 = int(round(_clamp(self.lon, -180.0, 180.0) * 1e7))
        # ce/le in decimetres, saturating at 6553.5 m -- beyond that the
        # number carries no operational meaning anyway.
        ce_dm = int(round(_clamp(self.ce, 0.0, 6553.5) * 10))
        le_dm = int(round(_clamp(self.le, 0.0, 6553.5) * 10))
        flags = FLAG_BEHIND_WALL if self.behind_wall else 0
        return _RECORD.pack(
            _uid_hash(self.uid),
            lat_e7,
            lon_e7,
            int(round(_clamp(self.alt, -32768, 32767))),
            ce_dm,
            le_dm,
            self.kind & 0xFF,
            QUALITY_CODES.get(self.quality, 3),
            int(round(_clamp(self.confidence, 0.0, 1.0) * 255)),
            flags,
        )

    @classmethod
    def unpack(cls, raw: bytes) -> "MeshRecord":
        (uid_hash, lat_e7, lon_e7, alt, ce_dm, le_dm,
         kind, quality, confidence, flags) = _RECORD.unpack(raw)
        return cls(
            uid=f"{uid_hash:08x}",      # the string is not recoverable, by design
            lat=lat_e7 / 1e7,
            lon=lon_e7 / 1e7,
            alt=float(alt),
            ce=ce_dm / 10.0,
            le=le_dm / 10.0,
            kind=kind,
            quality=QUALITY_NAMES.get(quality, "lost"),
            confidence=confidence / 255.0,
            behind_wall=bool(flags & FLAG_BEHIND_WALL),
        )


class MeshCodec:
    """Pack/unpack AURA positions into a single LoRa-sized frame."""

    HEADER_SIZE = _HEADER.size
    RECORD_SIZE = _RECORD.size

    @staticmethod
    def max_records(limit: int = MESHTASTIC_SAFE_PAYLOAD) -> int:
        return max(0, (limit - _HEADER.size) // _RECORD.size)

    @staticmethod
    def encode(records: Iterable[MeshRecord],
               epoch: float | None = None,
               limit: int = MESHTASTIC_SAFE_PAYLOAD) -> bytes:
        """Encode, truncating to fit one packet.

        Truncation is deliberate and ordered: the caller sorts by importance,
        because dropping a contact is better than emitting a frame the radio
        silently discards. `self` should always be first.
        """
        items = list(records)[: MeshCodec.max_records(limit)]
        header = _HEADER.pack(FRAME_MAGIC, FRAME_VERSION,
                              int(epoch if epoch is not None else time.time()),
                              len(items))
        return header + b"".join(r.pack() for r in items)

    @staticmethod
    def decode(payload: bytes) -> tuple[float, list[MeshRecord]]:
        if len(payload) < _HEADER.size:
            raise ValueError("frame shorter than header")
        magic, version, epoch, count = _HEADER.unpack(payload[: _HEADER.size])
        if magic != FRAME_MAGIC:
            raise ValueError(f"bad magic 0x{magic:02x}, not an AURA mesh frame")
        if version != FRAME_VERSION:
            raise ValueError(f"unsupported frame version {version}")
        body = payload[_HEADER.size:]
        if len(body) < count * _RECORD.size:
            raise ValueError("frame truncated: fewer records than the header claims")
        records = [
            MeshRecord.unpack(body[i * _RECORD.size:(i + 1) * _RECORD.size])
            for i in range(count)
        ]
        return float(epoch), records

    @staticmethod
    def from_cot_events(events: Iterable[Any]) -> list[MeshRecord]:
        """Convert `aura.cot.CotEvent` objects into wire records.

        Self events sort first so truncation never drops our own position.
        """
        records: list[MeshRecord] = []
        for event in events:
            is_self = getattr(event, "cot_type", "").startswith("a-f-")
            remarks = getattr(event, "remarks", "") or ""
            quality = "lost"
            for name in ("good", "degraded", "poor"):
                if f"quality={name}" in remarks:
                    quality = name
                    break
            records.append(MeshRecord(
                uid=getattr(event, "uid", "?"),
                lat=getattr(event, "lat", 0.0),
                lon=getattr(event, "lon", 0.0),
                alt=getattr(event, "hae", 0.0),
                ce=getattr(event, "ce", 0.0),
                le=getattr(event, "le", 0.0),
                kind=KIND_SELF if is_self else KIND_CONTACT,
                quality=quality,
                behind_wall="through-wall" in remarks,
            ))
        records.sort(key=lambda r: 0 if r.kind == KIND_SELF else 1)
        return records


@dataclass
class SendBudget:
    """Duty-cycle accountant and change filter.

    Two independent reasons to stay silent, both enforced here:

    * **Regulatory.** EN 300 220 allows 1% duty cycle on EU 868. Transmitting
      past that is not a performance question, it is unlawful.
    * **Pointless traffic.** A stationary operator re-sending an identical
      position spends budget that a moving contact will need.
    """

    duty_cycle: float = EU868_DUTY_CYCLE
    airtime_s: float = LONGFAST_AIRTIME_200B
    min_move_m: float = 5.0
    max_silence_s: float = 900.0     # heartbeat: prove we are alive
    _last_tx: float = field(default=0.0, repr=False)
    _last_pos: tuple[float, float] | None = field(default=None, repr=False)

    @property
    def required_gap_s(self) -> float:
        """Silence owed after one transmission."""
        return self.airtime_s / self.duty_cycle

    def packets_per_hour(self) -> float:
        return 3600.0 / self.required_gap_s

    def should_send(self, lat: float, lon: float, now: float | None = None) -> tuple[bool, str]:
        """Return (permitted, reason). The reason is for the operator log --
        silently dropping traffic in a tactical system is its own hazard."""
        now = time.time() if now is None else now
        elapsed = now - self._last_tx

        if self._last_tx and elapsed < self.required_gap_s:
            return False, (
                f"duty cycle: {self.required_gap_s - elapsed:.0f}s remaining "
                f"of the {self.required_gap_s:.0f}s owed after the last packet"
            )

        if self._last_pos is not None and elapsed < self.max_silence_s:
            moved = _haversine_m(self._last_pos[0], self._last_pos[1], lat, lon)
            if moved < self.min_move_m:
                return False, f"unchanged: moved {moved:.1f}m < {self.min_move_m}m"

        return True, "ok"

    def record_sent(self, lat: float, lon: float, now: float | None = None) -> None:
        self._last_tx = time.time() if now is None else now
        self._last_pos = (lat, lon)


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(min(1.0, a)))
