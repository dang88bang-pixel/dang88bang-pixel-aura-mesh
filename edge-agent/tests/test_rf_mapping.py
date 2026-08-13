"""Camera-free, LiDAR-free mapping from mmWave structure returns.

The premise of AURA is reconstructing an environment from radio, without a
camera and without line of sight. These tests pin down which radio path can
actually do that, and prove the map still builds when no LiDAR is attached.

The distinction that matters:

* RTI and Wi-Fi CSI subtract an empty-room baseline by construction, so they
  measure *change*. They locate people through walls; they cannot image a
  stationary wall. ``test_rti_is_blind_to_static_structure`` proves this
  against our own implementation rather than asserting it in prose.
* FMCW mmWave measures round-trip time across ~4 GHz of sweep, so a
  stationary return carries real geometry.
"""

from __future__ import annotations

import numpy as np
import pytest

from aura.config import AgentConfig
from aura.fusion import (
    MMWAVE_STATIC_MAX_RANGE,
    MMWAVE_STATIC_MIN_RANGE,
    MMWAVE_STATIC_MIN_SNR,
    MMWAVE_STATIC_VELOCITY,
    FusionPipeline,
)
from aura.rti import RtiGrid, RtiProcessor
from aura.sensors.mmwave import MmwaveTarget


# ----------------------------------------------------------------------
# the gate itself
# ----------------------------------------------------------------------


def test_static_gate_never_overlaps_the_people_gate():
    """A return must not be both 'wall' and 'person'.

    People are gated at |v| > 0.18 m/s; structure at |v| <= 0.05 m/s. The
    band between is deliberately unclaimed -- writing a walking person into
    the map as a wall is much worse than leaving the map sparse.
    """
    assert MMWAVE_STATIC_VELOCITY < 0.18


def test_static_thresholds_are_physically_sane():
    assert MMWAVE_STATIC_MIN_RANGE > 0.0
    assert MMWAVE_STATIC_MIN_RANGE < MMWAVE_STATIC_MAX_RANGE
    assert MMWAVE_STATIC_MIN_SNR > 0.0


# ----------------------------------------------------------------------
# the load-bearing claim: a map with no LiDAR and no camera
# ----------------------------------------------------------------------


def _pipeline_without_lidar() -> FusionPipeline:
    cfg = AgentConfig()
    cfg.simulate = True
    pipe = FusionPipeline(cfg)
    # Hard-disable the LiDAR: whatever the grid ends up containing cannot
    # have come from a laser.
    pipe.lidar.read = lambda: None          # type: ignore[assignment]
    pipe._pose_initialized = True
    return pipe


def _wall_returns(n: int = 24) -> list[MmwaveTarget]:
    """A flat wall 4 m ahead, spread across the azimuth, all stationary."""
    return [
        MmwaveTarget(x=4.0, y=-1.5 + 3.0 * i / max(n - 1, 1), z=0.0,
                     velocity=0.0, snr=25.0, track_id=-1)
        for i in range(n)
    ]


def test_map_builds_from_mmwave_alone_without_lidar():
    pipe = _pipeline_without_lidar()
    pipe.mmwave.read = lambda: _wall_returns()   # type: ignore[assignment]

    before = pipe.grid.updates
    for _ in range(6):
        pipe.tick()

    assert pipe.grid.updates > before, "no LiDAR and no camera -> still a map"
    assert pipe.grid.occupied_cells().size > 0


def test_moving_returns_do_not_become_walls():
    """People must not be painted into the structure map."""
    pipe = _pipeline_without_lidar()
    walkers = [
        MmwaveTarget(x=3.0, y=0.2 * i, z=0.0, velocity=1.1, snr=30.0, track_id=i)
        for i in range(8)
    ]
    pipe.mmwave.read = lambda: walkers            # type: ignore[assignment]

    for _ in range(6):
        pipe.tick()

    assert pipe.grid.occupied_cells().size == 0, "moving targets leaked into the map"


def test_low_snr_and_out_of_range_returns_are_rejected():
    pipe = _pipeline_without_lidar()
    junk = [
        # too close: antenna crosstalk
        MmwaveTarget(x=0.1, y=0.0, z=0.0, velocity=0.0, snr=40.0),
        # too far
        MmwaveTarget(x=MMWAVE_STATIC_MAX_RANGE + 5.0, y=0.0, z=0.0,
                     velocity=0.0, snr=40.0),
        # too noisy: multipath ghost
        MmwaveTarget(x=4.0, y=0.0, z=0.0, velocity=0.0,
                     snr=MMWAVE_STATIC_MIN_SNR - 1.0),
    ]
    pipe.mmwave.read = lambda: junk               # type: ignore[assignment]

    for _ in range(6):
        pipe.tick()

    assert pipe.grid.occupied_cells().size == 0


def test_structure_count_is_reported():
    """The payload must say how much of the map came from radar."""
    pipe = _pipeline_without_lidar()
    pipe.mmwave.read = lambda: _wall_returns()    # type: ignore[assignment]
    pipe.tick()

    frame = pipe.tick()
    payload = frame.get("mmwave") or {}
    assert payload.get("structure", 0) > 0, "structure count not reported"
    assert payload.get("mapped_cells", 0) > 0, "mapped cell count not reported"
    assert payload.get("moving", -1) == 0


# ----------------------------------------------------------------------
# why RTI cannot do this job
# ----------------------------------------------------------------------


def test_rti_is_blind_to_static_structure():
    """RTI subtracts the empty room, so a permanent wall is invisible to it.

    This is not a defect. It is why AURA needs mmWave for geometry and RTI
    for people, and why "reconstruct the building from Wi-Fi" does not
    follow from "detect a person through a wall with Wi-Fi".
    """
    nodes = {
        "n0": (0.0, 0.0), "n1": (6.0, 0.0), "n2": (6.0, 6.0),
        "n3": (0.0, 6.0), "n4": (3.0, 0.0), "n5": (3.0, 6.0),
    }
    grid = RtiGrid(min_x=0.0, min_y=0.0, max_x=6.0, max_y=6.0, resolution=0.5)
    proc = RtiProcessor(grid=grid, nodes=nodes)

    # A wall is present the entire time -- during calibration AND after. Its
    # attenuation is baked into every link reading, constantly.
    static_world = {
        pair: -60.0 - 3.0 * (idx % 4)
        for idx, pair in enumerate(proc.link_index)
    }

    proc.calibrate(static_world)
    result = proc.update(static_world)     # same room, nothing moved

    image = np.asarray(result.image)
    assert float(np.max(np.abs(image))) < 1e-6, (
        "RTI reported structure from an unchanged scene; it should report nothing"
    )
