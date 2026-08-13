"""Cursor-on-Target export.

The risky part of CoT is not the XML, it is the semantics: a wrong affiliation
paints an unknown contact as friendly, a wrong stale time leaves ghost tracks
on the map after the source is gone, and a missing geo anchor puts every
contact at lat=0/lon=0 in the Gulf of Guinea. These tests pin those down.
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import pytest

from aura.cot import (
    CE_UNKNOWN,
    COT_TYPE_CONTACT,
    COT_TYPE_SELF,
    CotEncoder,
    CotEvent,
    GeoAnchor,
    GeoAnchorError,
)


# ----------------------------------------------------------------------
# the geo anchor: the thing that cannot be guessed
# ----------------------------------------------------------------------


def test_anchor_rejects_impossible_coordinates():
    with pytest.raises(GeoAnchorError):
        GeoAnchor(lat=91.0, lon=0.0)
    with pytest.raises(GeoAnchorError):
        GeoAnchor(lat=0.0, lon=181.0)
    with pytest.raises(GeoAnchorError):
        GeoAnchor(lat=float("nan"), lon=0.0)


def test_encoder_refuses_to_work_without_an_anchor():
    """Better to fail loudly than to emit plausible XML at the wrong place."""
    with pytest.raises(GeoAnchorError):
        CotEncoder(anchor=None)          # type: ignore[arg-type]
    with pytest.raises(GeoAnchorError):
        CotEncoder(anchor="52.37,9.73")  # type: ignore[arg-type]


def test_anchor_parses_config_string():
    anchor = GeoAnchor.from_config("52.3759, 9.7320, 55.0, 90.0")
    assert anchor.lat == pytest.approx(52.3759)
    assert anchor.lon == pytest.approx(9.7320)
    assert anchor.hae == pytest.approx(55.0)
    assert anchor.yaw_deg == pytest.approx(90.0)

    short = GeoAnchor.from_config("52.3759,9.7320")
    assert short.hae == 0.0 and short.yaw_deg == 0.0

    with pytest.raises(GeoAnchorError):
        GeoAnchor.from_config("52.3759")
    with pytest.raises(GeoAnchorError):
        GeoAnchor.from_config("north,west")


# ----------------------------------------------------------------------
# the coordinate conversion, checked against known geodesy
# ----------------------------------------------------------------------


def test_origin_maps_to_the_anchor_itself():
    anchor = GeoAnchor(lat=52.3759, lon=9.7320, hae=55.0)
    lat, lon, hae = anchor.to_wgs84(0.0, 0.0, 0.0)
    assert lat == pytest.approx(52.3759, abs=1e-9)
    assert lon == pytest.approx(9.7320, abs=1e-9)
    assert hae == pytest.approx(55.0)


def test_metres_per_degree_matches_wgs84_at_hannover():
    """1 degree of latitude near 52.4 N is ~111.3 km on WGS84.

    Checks the conversion against an independent value rather than against
    itself. At 52.4 N the meridional degree is about 111 291 m.
    """
    anchor = GeoAnchor(lat=52.3759, lon=9.7320)
    # yaw=0 => +x is north (see GeoAnchor.to_wgs84).
    lat, _, _ = anchor.to_wgs84(111_291.0, 0.0)   # one degree north
    assert lat - anchor.lat == pytest.approx(1.0, abs=2e-3)


def test_east_west_scales_with_cos_latitude():
    """A degree of longitude shrinks as cos(lat). Equator vs 60 N is 2x."""
    equator = GeoAnchor(lat=0.0, lon=0.0)
    high = GeoAnchor(lat=60.0, lon=0.0)
    # yaw=0 => +y is east.
    _, lon_eq, _ = equator.to_wgs84(0.0, 1000.0)
    _, lon_hi, _ = high.to_wgs84(0.0, 1000.0)
    # Same 1000 m east produces ~twice the longitude change at 60 N.
    assert (lon_hi - high.lon) / (lon_eq - equator.lon) == pytest.approx(2.0, rel=0.01)


def test_yaw_rotates_the_local_frame():
    """yaw=0 puts +x north; yaw=90 rotates it to east."""
    north_up = GeoAnchor(lat=52.0, lon=9.0, yaw_deg=0.0)
    east_up = GeoAnchor(lat=52.0, lon=9.0, yaw_deg=90.0)

    lat_n, lon_n, _ = north_up.to_wgs84(100.0, 0.0)   # +x -> north
    lat_e, lon_e, _ = east_up.to_wgs84(100.0, 0.0)    # +x -> east

    assert lat_n > north_up.lat and lon_n == pytest.approx(north_up.lon, abs=1e-9)
    assert lon_e > east_up.lon and lat_e == pytest.approx(east_up.lat, abs=1e-9)


def test_conversion_rejects_non_finite_input():
    anchor = GeoAnchor(lat=52.0, lon=9.0)
    with pytest.raises(GeoAnchorError):
        anchor.to_wgs84(float("inf"), 0.0)


# ----------------------------------------------------------------------
# event semantics
# ----------------------------------------------------------------------


def _state(sigma: float = 0.4, quality: str = "good", sigma_z: float = 0.1) -> dict:
    """Mirrors the real EkfSnapshot payload.

    The field is `position_sigma` = [sx, sy, sz]; there is no `sigma_max` key.
    An earlier version of the encoder read `sigma_max`, silently got None and
    emitted ce=9999999 against a live agent -- hence this fixture and
    `test_reads_the_real_ekf_field_name` below.
    """
    return {"ekf": {"position": [10.0, 5.0, 1.2],
                    "position_sigma": [sigma, sigma * 0.9, sigma_z],
                    "quality": quality}}


def _encoder() -> CotEncoder:
    return CotEncoder(GeoAnchor(lat=52.3759, lon=9.7320, hae=55.0), callsign="AURA-1")


def test_self_event_is_wellformed_and_friendly():
    event = _encoder().self_event(_state())
    root = ET.fromstring(event.to_string())

    assert root.tag == "event"
    assert root.get("type") == COT_TYPE_SELF
    assert root.get("version") == "2.0"
    for attr in ("uid", "how", "time", "start", "stale"):
        assert root.get(attr), f"missing mandatory attribute {attr}"

    point = root.find("point")
    assert point is not None
    assert float(point.get("lat")) == pytest.approx(52.3759, abs=1e-3)


def test_detected_people_are_never_labelled_friendly():
    """AURA detects a body, not an allegiance."""
    people = [{"track_id": "p1", "x": 3.0, "y": 4.0, "confidence": 0.9,
               "behind_wall": True, "sources": ["mmwave"]}]
    events = _encoder().person_events(people)
    assert len(events) == 1
    assert events[0].cot_type == COT_TYPE_CONTACT
    # 'a-u-' is the unknown affiliation prefix; 'a-f-' would be friendly.
    assert events[0].cot_type.startswith("a-u-")
    assert not events[0].cot_type.startswith("a-f-")
    assert not events[0].cot_type.startswith("a-h-")


def test_reported_error_reflects_actual_uncertainty():
    """ce must come from the EKF, not the conventional 9999999 junk value."""
    good = _encoder().self_event(_state(sigma=0.2))
    poor = _encoder().self_event(_state(sigma=5.0, quality="poor"))
    assert good.ce < poor.ce
    assert good.ce != CE_UNKNOWN and poor.ce != CE_UNKNOWN
    # 1-sigma -> 95% circular error via the Rayleigh factor.
    assert good.ce == pytest.approx(0.2 * 2.4477, abs=0.01)


def test_reads_the_real_ekf_field_name():
    """Regression: the encoder must read `position_sigma`, not `sigma_max`.

    Reading a key the EKF does not publish degrades silently to the junk
    value 9999999, which looks like working CoT and destroys the one thing
    this module set out to do -- report honest accuracy.
    """
    event = _encoder().self_event(_state(sigma=0.5))
    assert event.ce != CE_UNKNOWN, "ce fell back to unknown: wrong EKF key?"
    assert event.ce == pytest.approx(0.5 * 2.4477, abs=0.01)


def test_vertical_error_uses_the_1d_factor():
    """le is a 1D interval (1.96 sigma), not the 2D Rayleigh factor."""
    event = _encoder().self_event(_state(sigma=0.4, sigma_z=0.25))
    assert event.ce != event.le
    assert event.le == pytest.approx(0.25 * 1.96, abs=0.01)


def test_unknown_sigma_falls_back_to_the_cot_unknown_value():
    event = _encoder().self_event(
        {"ekf": {"position": [0, 0, 0], "position_sigma": [float("nan"), float("nan"), 0.0]}}
    )
    assert event.ce == CE_UNKNOWN


def test_low_confidence_contacts_report_larger_error():
    people = [
        {"track_id": "a", "x": 1.0, "y": 1.0, "confidence": 0.95},
        {"track_id": "b", "x": 2.0, "y": 2.0, "confidence": 0.10},
    ]
    events = _encoder().person_events(people)
    assert events[1].ce > events[0].ce


def test_degrading_quality_shortens_the_stale_time():
    """A stale fix left on the map is worse than no fix."""
    enc = _encoder()
    good = enc.self_event(_state(quality="good"))
    poor = enc.self_event(_state(quality="poor"))
    lost = enc.self_event(_state(quality="lost"))
    assert good.stale_seconds > poor.stale_seconds > lost.stale_seconds


def test_stale_is_always_after_start():
    root = ET.fromstring(_encoder().self_event(_state()).to_string())
    start = datetime.strptime(root.get("start"), "%Y-%m-%dT%H:%M:%S.%fZ")
    stale = datetime.strptime(root.get("stale"), "%Y-%m-%dT%H:%M:%S.%fZ")
    assert stale > start


def test_uids_are_stable_across_messages_but_unique_per_track():
    """Track correlation downstream depends on UID stability."""
    enc = _encoder()
    first = enc.self_event(_state()).uid
    second = enc.self_event(_state()).uid
    assert first == second, "self UID must not change between messages"

    people = [{"track_id": "p1", "x": 0, "y": 0}, {"track_id": "p2", "x": 1, "y": 1}]
    uids = [e.uid for e in enc.person_events(people)]
    assert len(set(uids)) == 2
    assert enc.person_events(people)[0].uid == uids[0]


def test_multiple_events_serialise_to_valid_xml():
    enc = _encoder()
    events = enc.events_for_state(
        _state(), [{"track_id": "p1", "x": 1.0, "y": 2.0, "confidence": 0.8}]
    )
    root = ET.fromstring(CotEncoder.to_document(events))
    assert root.tag == "events"
    assert len(root.findall("event")) == 2


def test_single_event_is_emitted_bare_not_wrapped():
    """TAK expects a bare <event> on the wire."""
    enc = _encoder()
    doc = CotEncoder.to_document([enc.self_event(_state())])
    assert ET.fromstring(doc).tag == "event"


def test_remarks_flag_detections_as_unverified():
    people = [{"track_id": "p1", "x": 1.0, "y": 1.0, "confidence": 0.5}]
    assert "unverified" in _encoder().person_events(people)[0].remarks


# ----------------------------------------------------------------------
# anchor uncertainty: the term that dominates the exported accuracy
# ----------------------------------------------------------------------


def test_anchor_uncertainty_reaches_the_exported_ce():
    """Regression: a GNSS anchor must not export the EKF's optimistic sigma.

    The EKF can be certain to 0.06 m *relative to the origin*. If that origin
    came from a 5 m handheld fix, the absolute position is a 5 m position.
    Publishing the EKF number alone understates the error by ~80x and puts a
    confidently-wrong marker on a tactical display.
    """
    state = _state(sigma=0.25)
    surveyed = CotEncoder(GeoAnchor(52.3759, 9.7320, sigma_m=0.0)).self_event(state)
    handheld = CotEncoder(GeoAnchor(52.3759, 9.7320, sigma_m=5.0)).self_event(state)

    assert handheld.ce > surveyed.ce
    # 5 m anchor dominates a 0.25 m EKF sigma; expect ~5 m * 2.4477.
    assert handheld.ce == pytest.approx(math.hypot(0.25, 5.0) * 2.4477, abs=0.05)


def test_anchor_uncertainty_also_applies_to_contacts():
    people = [{"track_id": "p1", "x": 1.0, "y": 1.0, "confidence": 0.9}]
    near = CotEncoder(GeoAnchor(52.0, 9.0, sigma_m=0.0)).person_events(people)[0]
    far = CotEncoder(GeoAnchor(52.0, 9.0, sigma_m=8.0)).person_events(people)[0]
    assert far.ce > near.ce


def test_anchor_source_is_visible_to_the_consumer():
    """A surveyed origin and a phone fix must be distinguishable downstream."""
    event = CotEncoder(
        GeoAnchor(52.0, 9.0, sigma_m=3.0, source="gnss")
    ).self_event(_state())
    assert "gnss" in event.remarks
    assert "3.00" in event.remarks
    root = ET.fromstring(event.to_string())
    aura = root.find("./detail/aura")
    assert aura is not None
    assert aura.get("anchor_source") == "gnss"
    assert aura.get("anchor_sigma_m") == "3.00"


def test_negative_anchor_sigma_is_rejected():
    with pytest.raises(GeoAnchorError):
        GeoAnchor(52.0, 9.0, sigma_m=-1.0)


def test_config_string_carries_sigma():
    anchor = GeoAnchor.from_config("52.3759,9.7320,55.0,0.0,4.5")
    assert anchor.sigma_m == pytest.approx(4.5)
    # Omitted -> 0.0, i.e. "surveyed"; documented and deliberate for the env form.
    assert GeoAnchor.from_config("52.3759,9.7320").sigma_m == 0.0
