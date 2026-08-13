"""Cursor-on-Target (CoT) export.

CoT is the wire format the whole TAK ecosystem speaks -- ATAK, iTAK, WinTAK,
TAKX and TAK Server. Speaking it is what makes AURA interoperable with all of
them at once, which is why it is worth doing before any single-client plugin.

Scope, stated plainly
---------------------
This module **produces** CoT events. It does not open sockets, does not
implement the TAK Server streaming protocol, and does not do TLS enrolment.
Transport is deliberately left to the caller (file drop, UDP multicast, a TAK
Server client, Meshtastic). Generating correct XML is the part that has a
single right answer; transport depends on the deployment.

The geo-referencing problem
---------------------------
**CoT is absolutely geo-referenced. AURA's fusion output is not.**

The EKF works in a local metric frame whose origin is wherever the agent
happened to start. CoT ``<point>`` requires WGS84 latitude/longitude. There is
no way to invent that mapping: someone has to tell us where the local origin
sits on the Earth, and how the local +x axis is rotated relative to true north.

So :class:`GeoAnchor` is a required, explicit input. Without it this module
refuses to emit events rather than emitting them at lat=0/lon=0 -- which is in
the Gulf of Guinea, and which a tactical display would happily draw.

Accuracy is carried honestly
----------------------------
CoT has fields for this and they are usually filled with junk (``9999999.0``
is the conventional "unknown"). AURA actually knows its own uncertainty, so:

* ``ce`` (circular error, metres) is taken from the EKF covariance.
* ``le`` (linear error) is set from the vertical term, or unknown if we have
  no vertical observability.
* ``stale`` is short, because a position derived from dead reckoning decays.

A consumer that trusts ``ce`` will therefore be told the truth, including when
the truth is "this fix is poor".
"""

from __future__ import annotations

import math
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

# WGS84
_WGS84_A = 6378137.0                 # semi-major axis, m
_WGS84_F = 1.0 / 298.257223563       # flattening
_WGS84_E2 = _WGS84_F * (2.0 - _WGS84_F)

# CoT's conventional "I do not know" for ce/le.
CE_UNKNOWN = 9999999.0
LE_UNKNOWN = 9999999.0

# --- CoT type strings (MIL-STD-2525 derived) --------------------------
# Affiliation is the dangerous field. A wrong guess paints an unknown contact
# as friendly or hostile on someone's map, so AURA never guesses: a person
# detected by radar is 'a-u-G' -- atom, UNKNOWN affiliation, ground.
COT_TYPE_SELF = "a-f-G-U-C"      # friendly ground unit combat: the operator
COT_TYPE_CONTACT = "a-u-G"       # unknown ground contact: a detected person
COT_TYPE_SENSOR = "b-m-p-s-p-i"  # sensor point of interest
COT_TYPE_HAZARD = "b-m-p-a"      # point, hazard

# how: 'm-g' = machine-derived, GPS-like; 'm-r' = machine, radar-derived.
HOW_MACHINE_GEO = "m-g"
HOW_MACHINE_RADAR = "m-r"


class GeoAnchorError(ValueError):
    """Raised when CoT is requested without a usable geo anchor."""


@dataclass(frozen=True)
class GeoAnchor:
    """Ties AURA's local metric frame to the Earth.

    :param lat: WGS84 latitude of the local origin (0, 0), degrees.
    :param lon: WGS84 longitude of the local origin, degrees.
    :param hae: Height above the WGS84 *ellipsoid* of the origin, metres.
        Not height above mean sea level -- conflating the two is the classic
        source of tens of metres of vertical error.
    :param yaw_deg: Bearing of the local +x axis, degrees clockwise from
        **true** north. 0 means local +x points north. Note that a magnetic
        compass reading needs declination applied before it goes here.
    :param sigma_m: 1-sigma horizontal uncertainty of the anchor itself,
        metres. **This dominates the exported accuracy.** The EKF may know its
        position to 0.06 m relative to the origin, but if the origin was fixed
        by a handheld GNSS fix good to 5 m, every absolute position we publish
        is good to 5 m, not 0.06 m. Reporting the EKF sigma alone would
        understate the error by ~80x and put a confidently-wrong marker on
        someone's map. 0.0 means "surveyed point, error negligible".
    :param source: How the anchor was obtained -- ``manual``, ``gnss`` or
        ``surveyed``. Carried into the CoT ``how`` field and the remarks so a
        consumer can tell a surveyed origin from a phone fix.
    """

    lat: float
    lon: float
    hae: float = 0.0
    yaw_deg: float = 0.0
    sigma_m: float = 0.0
    source: str = "manual"

    def __post_init__(self) -> None:
        if not (-90.0 <= self.lat <= 90.0):
            raise GeoAnchorError(f"latitude out of range: {self.lat}")
        if not (-180.0 <= self.lon <= 180.0):
            raise GeoAnchorError(f"longitude out of range: {self.lon}")
        for name in ("lat", "lon", "hae", "yaw_deg", "sigma_m"):
            if not math.isfinite(getattr(self, name)):
                raise GeoAnchorError(f"{name} is not finite")
        if self.sigma_m < 0.0:
            raise GeoAnchorError(f"anchor sigma cannot be negative: {self.sigma_m}")

    def to_wgs84(self, x: float, y: float, z: float = 0.0) -> tuple[float, float, float]:
        """Local metres -> lat, lon, hae.

        Axis convention, which follows from ``yaw_deg`` being the bearing of
        the local **+x** axis: at ``yaw_deg=0``, **+x is north** and **+y is
        east**; ``z`` is up. At ``yaw_deg=90`` the frame is rotated so +x
        points east. (An earlier version of this docstring claimed the
        opposite; the code below is the authority and is what the tests pin.)

        Uses a local tangent-plane approximation with latitude-dependent
        metres-per-degree. Good to well under a metre over the tens of metres
        AURA maps, which is far below the position uncertainty we report in
        ``ce``, so a full geodesic solution would be false precision.
        """
        if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
            raise GeoAnchorError("non-finite local coordinate")

        # Rotate the local frame so +x points along the anchor bearing.
        theta = math.radians(self.yaw_deg)
        north = x * math.cos(theta) - y * math.sin(theta)
        east = x * math.sin(theta) + y * math.cos(theta)

        lat_rad = math.radians(self.lat)
        sin_lat = math.sin(lat_rad)
        # Meridional and normal radii of curvature at this latitude.
        denom = 1.0 - _WGS84_E2 * sin_lat * sin_lat
        m_per_deg_lat = (math.pi / 180.0) * _WGS84_A * (1.0 - _WGS84_E2) / (denom ** 1.5)
        m_per_deg_lon = (math.pi / 180.0) * _WGS84_A * math.cos(lat_rad) / math.sqrt(denom)

        lat = self.lat + north / m_per_deg_lat
        # Near the poles cos(lat) -> 0 and longitude becomes ill-conditioned.
        lon = self.lon + (east / m_per_deg_lon if abs(m_per_deg_lon) > 1e-6 else 0.0)

        lat = max(-90.0, min(90.0, lat))
        lon = ((lon + 180.0) % 360.0) - 180.0
        return lat, lon, self.hae + z

    @classmethod
    def from_config(cls, value: str) -> "GeoAnchor":
        """Parse ``"lat,lon[,hae[,yaw[,sigma_m]]]"`` -- the env-var form."""
        parts = [p.strip() for p in str(value).split(",") if p.strip()]
        if len(parts) < 2:
            raise GeoAnchorError(
                f"geo anchor needs at least 'lat,lon', got {value!r}"
            )
        try:
            nums = [float(p) for p in parts[:5]]
        except ValueError as exc:
            raise GeoAnchorError(f"geo anchor not numeric: {value!r}") from exc
        return cls(*nums)


def _iso(dt: datetime) -> str:
    """CoT timestamps are ISO 8601 UTC with a trailing Z."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _combine_sigma(relative: float | None, anchor_sigma: float) -> float | None:
    """Total horizontal 1-sigma = EKF (relative to origin) (+) anchor error.

    The two are independent, so they add in quadrature. This is the step that
    keeps the export honest: the EKF may be certain to 0.06 m *relative to the
    origin*, but if that origin came from a 5 m GNSS fix then the absolute
    position is a 5 m position. Publishing 0.06 m would understate the error
    by ~80x, and a tactical display would draw a confidently-wrong marker.
    """
    if relative is None or not math.isfinite(relative) or relative < 0:
        return None
    if not math.isfinite(anchor_sigma) or anchor_sigma <= 0:
        return relative
    return math.hypot(relative, anchor_sigma)


def _sigma_to_ce(sigma: float | None) -> float:
    """EKF 1-sigma -> CoT circular error.

    CoT's ``ce`` is conventionally a ~95% circular error, so a 2D Gaussian
    with per-axis sigma maps via the Rayleigh 95% factor 2.4477.
    """
    if sigma is None or not math.isfinite(sigma) or sigma < 0:
        return CE_UNKNOWN
    return round(sigma * 2.4477, 2)


@dataclass
class CotEvent:
    """One CoT event, ready to serialise."""

    uid: str
    cot_type: str
    lat: float
    lon: float
    hae: float = 0.0
    ce: float = CE_UNKNOWN
    le: float = LE_UNKNOWN
    how: str = HOW_MACHINE_GEO
    time: datetime | None = None
    stale_seconds: float = 60.0
    callsign: str = ""
    remarks: str = ""
    detail: dict[str, Any] | None = None

    def to_xml(self) -> ET.Element:
        now = self.time or datetime.now(timezone.utc)
        stale = now + timedelta(seconds=max(1.0, self.stale_seconds))

        event = ET.Element("event", {
            "version": "2.0",
            "uid": self.uid,
            "type": self.cot_type,
            "how": self.how,
            "time": _iso(now),
            "start": _iso(now),
            "stale": _iso(stale),
        })
        ET.SubElement(event, "point", {
            "lat": f"{self.lat:.8f}",
            "lon": f"{self.lon:.8f}",
            "hae": f"{self.hae:.2f}",
            "ce": f"{self.ce:.2f}",
            "le": f"{self.le:.2f}",
        })
        detail = ET.SubElement(event, "detail")
        if self.callsign:
            ET.SubElement(detail, "contact", {"callsign": self.callsign})
        if self.remarks:
            ET.SubElement(detail, "remarks").text = self.remarks
        for tag, attrs in (self.detail or {}).items():
            ET.SubElement(detail, tag, {k: str(v) for k, v in attrs.items()})
        return event

    def to_string(self) -> str:
        return ET.tostring(self.to_xml(), encoding="unicode")


class CotEncoder:
    """Turns AURA telemetry into CoT events.

    :param anchor: Required. See the module docstring on why.
    :param callsign: Callsign for this unit in the common operating picture.
    """

    def __init__(self, anchor: GeoAnchor, callsign: str = "AURA") -> None:
        if not isinstance(anchor, GeoAnchor):
            raise GeoAnchorError(
                "CoT export needs a GeoAnchor; AURA's local frame has no "
                "position on the Earth until one is supplied"
            )
        self.anchor = anchor
        self.callsign = callsign
        # UIDs must be stable for an entity's whole lifetime -- downstream
        # track correlation depends on it -- so derive them from a per-run
        # namespace plus the track id, never a fresh uuid4 per message.
        self._namespace = uuid.uuid4().hex[:12]

    def uid_for(self, kind: str, ident: str) -> str:
        return f"AURA-{self._namespace}-{kind}-{ident}"

    def self_event(self, state: dict[str, Any]) -> CotEvent:
        """The operator's own position, as a friendly ground unit."""
        ekf = state.get("ekf") or {}
        pos = ekf.get("position") or [0.0, 0.0, 0.0]
        x, y = float(pos[0]), float(pos[1])
        z = float(pos[2]) if len(pos) > 2 else 0.0

        # The EKF publishes per-axis 1-sigma as `position_sigma` = [sx, sy, sz].
        # Horizontal error is the worse of the two horizontal axes; the
        # vertical term is reported separately as `le` rather than discarded.
        sigmas = ekf.get("position_sigma") or []
        sigma = max(sigmas[0], sigmas[1]) if len(sigmas) >= 2 else ekf.get("sigma_max")
        sigma_z = sigmas[2] if len(sigmas) >= 3 else None
        quality = ekf.get("quality")
        lat, lon, hae = self.anchor.to_wgs84(x, y, z)

        # A degrading fix should go stale sooner: a 'lost' position is worse
        # than no position if a display keeps showing it as current.
        stale = {"good": 120.0, "degraded": 60.0, "poor": 30.0}.get(quality, 15.0)

        return CotEvent(
            uid=self.uid_for("self", "0"),
            cot_type=COT_TYPE_SELF,
            lat=lat, lon=lon, hae=hae,
            ce=_sigma_to_ce(_combine_sigma(sigma, self.anchor.sigma_m)),
            # Vertical: 1-sigma -> ~95% is the 1D factor 1.96, not the
            # Rayleigh 2.4477 used for the 2D circular error above.
            le=(round(sigma_z * 1.96, 2)
                if sigma_z is not None and math.isfinite(sigma_z) and sigma_z >= 0
                else LE_UNKNOWN),
            how=HOW_MACHINE_GEO,
            callsign=self.callsign,
            stale_seconds=stale,
            remarks=(
                f"AURA fusion; quality={quality or 'unknown'}; "
                f"anchor={self.anchor.source} +/-{self.anchor.sigma_m:.2f}m"
            ),
            detail={
                "__group": {"name": "Cyan", "role": "Team Member"},
                "precisionlocation": {"geopointsrc": "CALC", "altsrc": "CALC"},
                "aura": {
                    "quality": str(quality or "unknown"),
                    "anchor_source": self.anchor.source,
                    "anchor_sigma_m": f"{self.anchor.sigma_m:.2f}",
                },
            },
        )

    def person_events(self, people: Iterable[dict[str, Any]]) -> list[CotEvent]:
        """Detected people.

        Emitted with **unknown** affiliation. AURA detects a body, not an
        allegiance, and a tactical display colours from affiliation.
        """
        events: list[CotEvent] = []
        for person in people or []:
            track_id = str(person.get("track_id", "?"))
            lat, lon, hae = self.anchor.to_wgs84(
                float(person.get("x", 0.0)), float(person.get("y", 0.0)), 0.0
            )
            confidence = float(person.get("confidence", 0.0) or 0.0)
            behind = bool(person.get("behind_wall"))
            sources = ",".join(person.get("sources") or [])

            # Lower confidence -> larger reported error. A detection we are
            # 30% sure of must not arrive looking like a survey point.
            # Detection uncertainty, then the anchor error on top of it.
            detection_sigma = 1.5 + 6.0 * (1.0 - min(max(confidence, 0.0), 1.0))
            ce = round(math.hypot(detection_sigma, self.anchor.sigma_m), 2)

            events.append(CotEvent(
                uid=self.uid_for("contact", track_id),
                cot_type=COT_TYPE_CONTACT,
                lat=lat, lon=lon, hae=hae,
                ce=ce, le=LE_UNKNOWN,
                how=HOW_MACHINE_RADAR,
                callsign=f"Contact-{track_id}",
                stale_seconds=30.0,
                remarks=(
                    f"unverified detection; confidence={confidence:.2f}; "
                    f"sources={sources or 'none'}"
                    + ("; through-wall" if behind else "")
                ),
                detail={
                    "aura": {
                        "confidence": f"{confidence:.3f}",
                        "behind_wall": "true" if behind else "false",
                        "sources": sources,
                    },
                },
            ))
        return events

    def events_for_state(self, state: dict[str, Any],
                         people: Iterable[dict[str, Any]] | None = None) -> list[CotEvent]:
        events = [self.self_event(state)]
        events.extend(self.person_events(people or []))
        return events

    @staticmethod
    def to_document(events: Iterable[CotEvent]) -> str:
        """Serialise events.

        A single event is emitted bare, because that is what TAK expects on
        the wire. Several are wrapped in ``<events>`` -- valid XML for a file
        drop, but note it is *not* a streaming frame format.
        """
        items = list(events)
        if len(items) == 1:
            return items[0].to_string()
        root = ET.Element("events")
        for event in items:
            root.append(event.to_xml())
        return ET.tostring(root, encoding="unicode")
