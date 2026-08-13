"""Mesh transport codec: size budget, duty cycle, round-trip fidelity.

The claim being tested is not "the code runs" but "the frame fits a LoRa
packet and the radio is not driven past what EN 300 220 permits".
"""

from __future__ import annotations

import math
import struct

import pytest

from aura.cot import CotEncoder, GeoAnchor
from aura.mesh import (
    EU868_DUTY_CYCLE,
    FLAG_BEHIND_WALL,
    KIND_CONTACT,
    KIND_SELF,
    MESHTASTIC_MAX_PAYLOAD,
    MESHTASTIC_SAFE_PAYLOAD,
    MeshCodec,
    MeshRecord,
    SendBudget,
    _haversine_m,
)


# ----------------------------------------------------------------------
# the constraint that motivates the whole module
# ----------------------------------------------------------------------


def test_cot_xml_does_not_fit_a_lora_packet():
    """The premise: this is why a binary codec exists at all.

    A single CoT self-event is ~470 bytes against a ~200 byte practical
    budget. If this ever stops being true the codec's justification changes.
    """
    encoder = CotEncoder(GeoAnchor(lat=52.3759, lon=9.7320), callsign="AURA-1")
    state = {"ekf": {"position": [10.0, 5.0, 1.2],
                     "position_sigma": [0.3, 0.3, 0.1],
                     "quality": "good"}}
    xml = encoder.self_event(state).to_string()
    assert len(xml) > MESHTASTIC_SAFE_PAYLOAD, (
        "CoT XML now fits a LoRa packet; re-check whether this codec is needed"
    )


def test_binary_frame_fits_where_xml_does_not():
    records = [
        MeshRecord(uid=f"c{i}", lat=52.0 + i * 1e-4, lon=9.0, kind=KIND_CONTACT)
        for i in range(8)
    ]
    records[0].kind = KIND_SELF
    frame = MeshCodec.encode(records)
    assert len(frame) <= MESHTASTIC_SAFE_PAYLOAD
    assert len(frame) <= MESHTASTIC_MAX_PAYLOAD


def test_record_size_is_what_the_budget_assumes():
    assert MeshCodec.RECORD_SIZE == 22
    assert MeshCodec.HEADER_SIZE == 8
    # self + 7 contacts must fit the safe budget.
    assert MeshCodec.HEADER_SIZE + 8 * MeshCodec.RECORD_SIZE <= MESHTASTIC_SAFE_PAYLOAD


def test_oversized_input_is_truncated_not_emitted_over_budget():
    """A frame the radio drops is worse than a frame missing a contact."""
    records = [MeshRecord(uid=f"c{i}", lat=52.0, lon=9.0) for i in range(50)]
    frame = MeshCodec.encode(records)
    assert len(frame) <= MESHTASTIC_SAFE_PAYLOAD
    _, decoded = MeshCodec.decode(frame)
    assert len(decoded) == MeshCodec.max_records()


def test_self_is_never_the_record_that_gets_dropped():
    encoder = CotEncoder(GeoAnchor(lat=52.3759, lon=9.7320))
    state = {"ekf": {"position": [1.0, 2.0, 0.0],
                     "position_sigma": [0.2, 0.2, 0.1], "quality": "good"}}
    people = [{"track_id": f"p{i}", "x": float(i), "y": 0.0, "confidence": 0.5}
              for i in range(40)]
    events = encoder.events_for_state(state, people)
    records = MeshCodec.from_cot_events(events)
    frame = MeshCodec.encode(records)
    _, decoded = MeshCodec.decode(frame)
    assert decoded[0].kind == KIND_SELF


# ----------------------------------------------------------------------
# round trip
# ----------------------------------------------------------------------


def test_round_trip_preserves_position_to_better_than_reported_error():
    original = MeshRecord(uid="self-0", lat=52.3759123, lon=9.7320456,
                          alt=56.0, ce=0.62, le=0.21, kind=KIND_SELF,
                          quality="good", confidence=0.87, behind_wall=True)
    _, decoded = MeshCodec.decode(MeshCodec.encode([original]))
    got = decoded[0]

    # 1e-7 degrees is ~1.1 cm -- two orders below the ce we report.
    assert got.lat == pytest.approx(original.lat, abs=1e-7)
    assert got.lon == pytest.approx(original.lon, abs=1e-7)
    assert got.ce == pytest.approx(original.ce, abs=0.05)
    assert got.le == pytest.approx(original.le, abs=0.05)
    assert got.quality == "good"
    assert got.kind == KIND_SELF
    assert got.behind_wall is True
    assert got.confidence == pytest.approx(0.87, abs=0.01)


def test_quality_survives_the_wire():
    for name in ("good", "degraded", "poor", "lost"):
        rec = MeshRecord(uid="x", lat=52.0, lon=9.0, quality=name)
        _, decoded = MeshCodec.decode(MeshCodec.encode([rec]))
        assert decoded[0].quality == name


def test_non_finite_and_out_of_range_values_do_not_corrupt_the_frame():
    """Garbage in must not produce an unparseable frame."""
    rec = MeshRecord(uid="x", lat=float("nan"), lon=1e9,
                     ce=float("inf"), confidence=5.0)
    frame = MeshCodec.encode([rec])
    _, decoded = MeshCodec.decode(frame)
    assert -90.0 <= decoded[0].lat <= 90.0
    assert -180.0 <= decoded[0].lon <= 180.0
    assert math.isfinite(decoded[0].ce)
    assert 0.0 <= decoded[0].confidence <= 1.0


def test_empty_frame_is_valid():
    _, decoded = MeshCodec.decode(MeshCodec.encode([]))
    assert decoded == []


# ----------------------------------------------------------------------
# malformed input
# ----------------------------------------------------------------------


def test_foreign_payload_is_rejected_not_misparsed():
    with pytest.raises(ValueError, match="magic"):
        MeshCodec.decode(b"\x00" * 32)


def test_truncated_frame_is_rejected():
    frame = MeshCodec.encode([MeshRecord(uid="a", lat=52.0, lon=9.0),
                              MeshRecord(uid="b", lat=52.1, lon=9.1)])
    with pytest.raises(ValueError, match="truncated"):
        MeshCodec.decode(frame[:-10])


def test_short_payload_is_rejected():
    with pytest.raises(ValueError):
        MeshCodec.decode(b"\xa6")


def test_future_version_is_refused():
    bad = struct.pack("<BBIH", 0xA6, 99, 0, 0)
    with pytest.raises(ValueError, match="version"):
        MeshCodec.decode(bad)


# ----------------------------------------------------------------------
# duty cycle: the actual binding constraint
# ----------------------------------------------------------------------


def test_duty_cycle_matches_en300220():
    budget = SendBudget()
    assert budget.duty_cycle == EU868_DUTY_CYCLE
    # 1.4 s airtime at 1% obliges 140 s of silence.
    assert budget.required_gap_s == pytest.approx(140.0, rel=0.01)
    # ~26 packets per hour, per sender. Not 10 Hz.
    assert budget.packets_per_hour() == pytest.approx(25.7, rel=0.05)


def test_second_transmission_is_refused_inside_the_duty_window():
    budget = SendBudget()
    ok, _ = budget.should_send(52.0, 9.0, now=1000.0)
    assert ok
    budget.record_sent(52.0, 9.0, now=1000.0)

    ok, reason = budget.should_send(52.001, 9.0, now=1010.0)
    assert not ok and "duty cycle" in reason

    ok, _ = budget.should_send(52.001, 9.0, now=1000.0 + 141.0)
    assert ok


def test_stationary_operator_does_not_burn_the_budget():
    budget = SendBudget()
    budget.record_sent(52.0, 9.0, now=1000.0)
    # Duty cycle satisfied, but nothing moved.
    ok, reason = budget.should_send(52.0, 9.0, now=1000.0 + 200.0)
    assert not ok and "unchanged" in reason


def test_movement_beyond_the_threshold_is_sent():
    budget = SendBudget()
    budget.record_sent(52.0, 9.0, now=1000.0)
    # ~11 m north, well past the 5 m gate.
    ok, _ = budget.should_send(52.0001, 9.0, now=1000.0 + 200.0)
    assert ok


def test_heartbeat_fires_even_when_stationary():
    """Silence must not be ambiguous between 'not moving' and 'dead'."""
    budget = SendBudget()
    budget.record_sent(52.0, 9.0, now=1000.0)
    ok, _ = budget.should_send(52.0, 9.0, now=1000.0 + 1000.0)
    assert ok, "no heartbeat after max_silence_s"


def test_first_transmission_is_always_permitted():
    ok, _ = SendBudget().should_send(52.0, 9.0, now=0.0)
    assert ok


def test_haversine_is_sane():
    # 0.001 deg latitude is ~111 m.
    assert _haversine_m(52.0, 9.0, 52.001, 9.0) == pytest.approx(111.2, rel=0.02)
    assert _haversine_m(52.0, 9.0, 52.0, 9.0) == 0.0


# ----------------------------------------------------------------------
# integration with the CoT layer
# ----------------------------------------------------------------------


def test_cot_events_convert_and_keep_their_meaning():
    encoder = CotEncoder(GeoAnchor(lat=52.3759, lon=9.7320, hae=55.0))
    state = {"ekf": {"position": [10.0, 5.0, 1.2],
                     "position_sigma": [0.25, 0.25, 0.1], "quality": "good"}}
    people = [{"track_id": "p1", "x": 3.0, "y": 4.0, "confidence": 0.9,
               "behind_wall": True, "sources": ["mmwave"]}]

    records = MeshCodec.from_cot_events(encoder.events_for_state(state, people))
    assert len(records) == 2
    assert records[0].kind == KIND_SELF
    assert records[0].quality == "good"
    assert records[1].kind == KIND_CONTACT
    assert records[1].behind_wall is True

    frame = MeshCodec.encode(records)
    assert len(frame) <= MESHTASTIC_SAFE_PAYLOAD
    _, decoded = MeshCodec.decode(frame)
    assert decoded[0].lat == pytest.approx(records[0].lat, abs=1e-7)


def test_compression_ratio_against_xml_is_real():
    encoder = CotEncoder(GeoAnchor(lat=52.3759, lon=9.7320))
    state = {"ekf": {"position": [1.0, 2.0, 0.0],
                     "position_sigma": [0.2, 0.2, 0.1], "quality": "good"}}
    people = [{"track_id": f"p{i}", "x": float(i), "y": 1.0, "confidence": 0.7}
              for i in range(3)]
    events = encoder.events_for_state(state, people)

    xml_size = len(CotEncoder.to_document(events))
    bin_size = len(MeshCodec.encode(MeshCodec.from_cot_events(events)))

    assert bin_size < xml_size / 10, (
        f"expected >10x reduction, got {xml_size} -> {bin_size}"
    )
    assert bin_size <= MESHTASTIC_SAFE_PAYLOAD
