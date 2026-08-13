"""AURA 6.0 subsystems: RTI compressed sensing, passive radar, voxels, audit."""

from __future__ import annotations

import math
import sqlite3

import numpy as np
import pytest

from aura.audit import AuditEntry, AuditStore, CausalValidator, canonical_json
from aura.passive_radar import (
    doppler_resolution,
    required_samples_for_doppler,
    SPEED_OF_LIGHT,
    bistatic_ellipse_point,
    cancellation_ratio_db,
    cfar_2d,
    compute_caf,
    compute_caf_fft,
    detect_targets,
    eca_cancel,
    generate_ofdm_reference,
    range_resolution,
    simulate_surveillance,
    velocity_resolution,
)
from aura.rti import (
    RtiGrid,
    RtiProcessor,
    build_weight_matrix,
    extract_targets,
    power_iteration_lipschitz,
    reconstruct_l1_fista,
    reconstruct_tikhonov,
    simulate_measurements,
    soft_threshold,
)
from aura.voxel import (
    CHUNK_SIZE,
    LABEL_DEVICE,
    LABEL_PERSON,
    LABEL_STRUCTURE,
    SparseVoxelOctree,
    VoxelChunk,
    VoxelWorld,
)


# ======================================================================
# RTI
# ======================================================================
def ring_of_nodes(count: int, center=(3.0, 3.0), radius: float = 3.2) -> np.ndarray:
    angles = np.linspace(0, 2 * np.pi, count, endpoint=False)
    return np.column_stack([center[0] + radius * np.cos(angles), center[1] + radius * np.sin(angles)])


def test_grid_geometry():
    grid = RtiGrid(0, 0, 6, 6, 0.25)
    assert grid.nx == 24 and grid.ny == 24
    assert grid.voxel_count == 576
    assert grid.index_of(-1, 3) is None
    assert grid.index_of(0.1, 0.1) == 0
    centers = grid.centers()
    assert centers.shape == (576, 2)
    assert centers[0][0] == pytest.approx(0.125)


def test_weight_matrix_shape_and_support():
    nodes = ring_of_nodes(8)
    grid = RtiGrid(0, 0, 6, 6, 0.25)
    W, links = build_weight_matrix(grid, nodes)
    assert len(links) == 8 * 7 // 2
    assert W.shape == (len(links), grid.voxel_count)
    assert np.count_nonzero(W) > 0
    # voxels on a link's line-of-sight must carry weight
    assert W.sum(axis=1).min() > 0


def test_soft_threshold_semantics():
    x = np.array([-2.0, -0.4, 0.0, 0.4, 2.0])
    out = soft_threshold(x, 0.5)
    assert out.tolist() == pytest.approx([-1.5, 0.0, 0.0, 0.0, 1.5])


def test_power_iteration_beats_frobenius_bound():
    nodes = ring_of_nodes(10)
    grid = RtiGrid(0, 0, 6, 6, 0.3)
    W, _ = build_weight_matrix(grid, nodes)
    L = power_iteration_lipschitz(W)
    true_L = float(np.linalg.eigvalsh(W.T @ W).max())
    assert L == pytest.approx(true_L, rel=0.02)
    # the naive ||W||_F^2 bound is valid but far too pessimistic
    assert L < np.linalg.norm(W) ** 2


def test_fista_recovers_a_single_target():
    nodes = ring_of_nodes(12)
    grid = RtiGrid(0, 0, 6, 6, 0.25)
    W, _ = build_weight_matrix(grid, nodes)
    truth = np.zeros(grid.voxel_count)
    truth[grid.index_of(2.0, 4.0)] = 1.0
    y = simulate_measurements(W, truth, noise_sigma=0.01, rng=np.random.default_rng(2))

    result = reconstruct_l1_fista(W, y, alpha=0.05, max_iter=300)
    targets = extract_targets(grid, result.image)
    assert targets, "no target reconstructed"
    error = math.hypot(targets[0].x - 2.0, targets[0].y - 4.0)
    assert error < 0.6, f"localisation error {error:.2f} m"


def test_fista_is_sparser_than_tikhonov():
    nodes = ring_of_nodes(12)
    grid = RtiGrid(0, 0, 6, 6, 0.25)
    W, _ = build_weight_matrix(grid, nodes)
    truth = np.zeros(grid.voxel_count)
    truth[grid.index_of(3.0, 3.0)] = 1.0
    y = simulate_measurements(W, truth, noise_sigma=0.005, rng=np.random.default_rng(3))

    l1 = reconstruct_l1_fista(W, y, alpha=0.05, max_iter=250).image
    l2 = np.maximum(reconstruct_tikhonov(W, y, alpha=0.1), 0.0)
    l1_nnz = np.count_nonzero(l1 > 0.05 * l1.max())
    l2_nnz = np.count_nonzero(l2 > 0.05 * l2.max())
    assert l1_nnz < l2_nnz, "L1 should concentrate energy compared to L2"


def test_fista_objective_decreases_monotonically_overall():
    nodes = ring_of_nodes(10)
    grid = RtiGrid(0, 0, 6, 6, 0.3)
    W, _ = build_weight_matrix(grid, nodes)
    truth = np.zeros(grid.voxel_count)
    truth[grid.index_of(4.0, 2.0)] = 1.0
    y = simulate_measurements(W, truth)
    result = reconstruct_l1_fista(W, y, alpha=0.05, max_iter=150)
    assert result.objective[-1] < result.objective[0]
    assert result.lipschitz > 0


def test_fista_non_negativity_is_enforced():
    nodes = ring_of_nodes(8)
    grid = RtiGrid(0, 0, 6, 6, 0.4)
    W, _ = build_weight_matrix(grid, nodes)
    rng = np.random.default_rng(4)
    y = rng.normal(0, 1, W.shape[0])       # nonsense data with negative values
    result = reconstruct_l1_fista(W, y, alpha=0.05, max_iter=60, non_negative=True)
    assert result.image.min() >= 0.0


def test_fista_resolves_two_separated_targets():
    nodes = ring_of_nodes(16)
    grid = RtiGrid(0, 0, 6, 6, 0.2)
    W, _ = build_weight_matrix(grid, nodes)
    truth = np.zeros(grid.voxel_count)
    truth[grid.index_of(1.5, 1.5)] = 1.0
    truth[grid.index_of(4.5, 4.5)] = 1.0
    y = simulate_measurements(W, truth, noise_sigma=0.01, rng=np.random.default_rng(5))
    result = reconstruct_l1_fista(W, y, alpha=0.04, max_iter=300)
    targets = extract_targets(grid, result.image, threshold_ratio=0.35)
    assert len(targets) >= 2
    positions = sorted((t.x, t.y) for t in targets[:2])
    assert math.dist(positions[0], (1.5, 1.5)) < 0.9
    assert math.dist(positions[1], (4.5, 4.5)) < 0.9


def test_extract_targets_on_blank_image():
    grid = RtiGrid(0, 0, 4, 4, 0.5)
    assert extract_targets(grid, np.zeros(grid.voxel_count)) == []


def test_rti_processor_calibration_and_update():
    nodes = {f"n{i}": tuple(p) for i, p in enumerate(ring_of_nodes(12))}
    grid = RtiGrid(0, 0, 6, 6, 0.25)
    processor = RtiProcessor(grid, nodes, alpha=0.05)
    assert not processor.calibrated

    # empty room: every link sees a clean -50 dBm
    baseline = {(a, b): -50.0 for a in nodes for b in nodes if a < b}
    for _ in range(5):
        processor.calibrate(baseline)
    assert processor.calibrated

    # a person at (2, 4) attenuates the links that cross that voxel
    truth = np.zeros(grid.voxel_count)
    truth[grid.index_of(2.0, 4.0)] = 1.0
    attenuation = processor.W @ truth
    occupied = dict(baseline)
    for index, (a, b) in enumerate(processor.links):
        key = (processor.node_ids[a], processor.node_ids[b])
        occupied[key] = -50.0 - float(attenuation[index])

    processor.update(occupied, max_iter=250)
    targets = processor.targets()
    assert targets
    assert math.hypot(targets[0].x - 2.0, targets[0].y - 4.0) < 0.7
    payload = processor.as_dict()
    assert payload["calibrated"] and payload["links"] == len(processor.links)


def test_rti_processor_reports_nothing_for_an_empty_room():
    nodes = {f"n{i}": tuple(p) for i, p in enumerate(ring_of_nodes(10))}
    processor = RtiProcessor(RtiGrid(0, 0, 6, 6, 0.3), nodes)
    baseline = {(a, b): -50.0 for a in nodes for b in nodes if a < b}
    processor.calibrate(baseline)
    processor.update(baseline)
    assert processor.targets() == []


# ======================================================================
# passive radar
# ======================================================================
def test_resolution_formulas_are_physical():
    assert range_resolution(2.4e6) == pytest.approx(62.5, rel=0.01)
    assert range_resolution(8e6) == pytest.approx(18.7, rel=0.02)
    # a longer dwell must give finer velocity resolution
    assert velocity_resolution(626e6, 1.0) < velocity_resolution(626e6, 0.1)


def test_eca_removes_the_direct_path():
    rng = np.random.default_rng(6)
    ref = generate_ofdm_reference(8192, 2.4e6, rng=rng)
    surv = simulate_surveillance(ref, 2.4e6, [(20, 60.0, 0.01)], direct_path_gain=500.0)
    clean = eca_cancel(surv, ref, num_taps=16)
    suppression = cancellation_ratio_db(surv, clean)
    assert suppression > 30.0, f"only {suppression:.1f} dB of cancellation"
    assert np.mean(np.abs(clean) ** 2) < np.mean(np.abs(surv) ** 2)


def test_eca_preserves_a_doppler_shifted_echo():
    rng = np.random.default_rng(7)
    ref = generate_ofdm_reference(16384, 2.4e6, rng=rng)
    surv = simulate_surveillance(ref, 2.4e6, [(25, 90.0, 0.05)], direct_path_gain=300.0)
    clean = eca_cancel(surv, ref, num_taps=16)
    rd = compute_caf(clean, ref, 2.4e6, max_range_bins=48, doppler_bins=81,
                     max_doppler_hz=150.0, batch_decimation=4)
    detections = detect_targets(rd, threshold_db=8.0)
    assert detections, "the moving target must survive clutter cancellation"
    assert detections[0].range_bin == 25
    assert detections[0].doppler_hz == pytest.approx(90.0, abs=6.0)


def test_caf_locates_two_targets():
    rng = np.random.default_rng(8)
    fs = 2.4e6
    ref = generate_ofdm_reference(32768, fs, rng=rng)
    surv = simulate_surveillance(
        ref, fs, [(20, 80.0, 0.02), (45, -120.0, 0.01)], direct_path_gain=1000.0,
        noise_sigma=0.005, rng=rng,
    )
    clean = eca_cancel(surv, ref, num_taps=16)
    rd = compute_caf(clean, ref, fs, max_range_bins=64, doppler_bins=101,
                     max_doppler_hz=200.0, batch_decimation=4)
    detections = detect_targets(rd, threshold_db=10.0)
    bins = {(d.range_bin, round(d.doppler_hz)) for d in detections[:6]}
    assert any(b[0] == 20 and abs(b[1] - 80) < 8 for b in bins)
    assert any(b[0] == 45 and abs(b[1] + 120) < 8 for b in bins)


def test_caf_range_axis_is_calibrated():
    rng = np.random.default_rng(9)
    fs = 2.4e6
    ref = generate_ofdm_reference(8192, fs, rng=rng)
    surv = simulate_surveillance(ref, fs, [(10, 50.0, 0.05)], direct_path_gain=0.0)
    rd = compute_caf(surv, ref, fs, max_range_bins=32, doppler_bins=41,
                     max_doppler_hz=100.0, batch_decimation=4)
    expected = 10 * SPEED_OF_LIGHT / fs
    assert rd.range_bins[10] == pytest.approx(expected, rel=1e-6)


def test_caf_batches_variant_finds_the_range_bin():
    rng = np.random.default_rng(10)
    fs = 2.4e6
    ref = generate_ofdm_reference(16384, fs, rng=rng)
    surv = simulate_surveillance(ref, fs, [(15, 70.0, 0.05)], direct_path_gain=0.0)
    rd = compute_caf_fft(surv, ref, fs, max_range_bins=32, num_batches=32)
    peak = np.unravel_index(int(np.argmax(rd.magnitude)), rd.magnitude.shape)
    assert peak[1] == 15
    # 16384 samples at 2.4 MSps is a 6.8 ms dwell -> ~146 Hz Doppler cells,
    # so a 70 Hz target legitimately lands in the zero-Doppler bin.
    assert doppler_resolution(rd.integration_time) > 70.0


def test_doppler_resolution_requires_a_long_enough_dwell():
    fs = 2.4e6
    assert doppler_resolution(16384 / fs) == pytest.approx(146.5, rel=0.01)
    needed = required_samples_for_doppler(70.0, fs)
    assert needed > 16384

    # with a dwell that actually satisfies the limit the peak lands correctly
    rng = np.random.default_rng(21)
    n = 1 << int(math.ceil(math.log2(needed * 4)))
    ref = generate_ofdm_reference(n, fs, rng=rng)
    surv = simulate_surveillance(ref, fs, [(15, 70.0, 0.05)], direct_path_gain=0.0)
    rd = compute_caf_fft(surv, ref, fs, max_range_bins=24, num_batches=64)
    assert doppler_resolution(rd.integration_time) < 70.0
    peak = np.unravel_index(int(np.argmax(rd.magnitude)), rd.magnitude.shape)
    assert peak[1] == 15
    assert rd.doppler_bins[peak[0]] == pytest.approx(70.0, abs=25.0)


def test_velocity_conversion():
    rng = np.random.default_rng(11)
    ref = generate_ofdm_reference(4096, 2.4e6, rng=rng)
    surv = simulate_surveillance(ref, 2.4e6, [(5, 100.0, 0.05)], direct_path_gain=0.0)
    rd = compute_caf(surv, ref, 2.4e6, max_range_bins=16, doppler_bins=21,
                     max_doppler_hz=150.0, carrier_hz=626e6, batch_decimation=2)
    velocities = rd.velocity_bins()
    wavelength = SPEED_OF_LIGHT / 626e6
    assert velocities[-1] == pytest.approx(150.0 * wavelength / 2, rel=1e-6)


def test_cfar_finds_a_peak_and_ignores_flat_noise():
    rng = np.random.default_rng(12)
    noise = np.abs(rng.normal(0, 1, (40, 40)))
    assert np.count_nonzero(cfar_2d(noise, threshold_db=15.0)) <= 2

    with_target = noise.copy()
    with_target[20, 20] = 60.0
    mask = cfar_2d(with_target, threshold_db=15.0)
    assert mask[20, 20]


def test_rd_map_serialisation():
    rng = np.random.default_rng(13)
    ref = generate_ofdm_reference(4096, 2.4e6, rng=rng)
    surv = simulate_surveillance(ref, 2.4e6, [(8, 40.0, 0.05)], direct_path_gain=0.0)
    rd = compute_caf(surv, ref, 2.4e6, max_range_bins=16, doppler_bins=21,
                     max_doppler_hz=100.0, batch_decimation=4)
    payload = rd.as_dict(decimate=2)
    import json

    json.dumps(payload)
    assert payload["shape"][0] == len(payload["doppler_bins"])
    assert payload["shape"][1] == len(payload["range_bins"])
    assert rd.to_db().max() == pytest.approx(0.0, abs=1e-9)


def test_bistatic_ellipse_geometry():
    tx, rx = (0.0, 0.0), (10.0, 0.0)
    bistatic = 6.0
    for angle in (0.0, 1.0, 2.5, 4.0):
        point = bistatic_ellipse_point(tx, rx, bistatic, angle)
        assert point is not None
        total = math.dist(tx, point) + math.dist(point, rx)
        assert total == pytest.approx(math.dist(tx, rx) + bistatic, rel=1e-6)


def test_caf_rejects_too_short_records():
    with pytest.raises(ValueError):
        compute_caf(np.zeros(4), np.zeros(4), 2.4e6)


# ======================================================================
# voxels
# ======================================================================
def test_chunk_rle_roundtrip():
    chunk = VoxelChunk(0, 0, 0)
    chunk.set(1, 2, 3, 40000, LABEL_PERSON)
    chunk.set(5, 5, 5, 12345, LABEL_DEVICE)
    blob = chunk.to_bytes()
    restored = VoxelChunk.from_bytes(0, 0, 0, blob)
    assert np.array_equal(chunk.intensity, restored.intensity)
    assert np.array_equal(chunk.label, restored.label)
    assert restored.get(1, 2, 3) == (40000, LABEL_PERSON)


def test_empty_chunk_compresses_tiny():
    chunk = VoxelChunk(0, 0, 0)
    assert len(chunk.to_bytes()) < 40
    assert chunk.occupied == 0


def test_chunk_rejects_garbage():
    with pytest.raises(ValueError):
        VoxelChunk.from_bytes(0, 0, 0, b"not a chunk at all")


def test_negative_coordinates_map_to_distinct_chunks():
    world = VoxelWorld(voxel_size=0.1, chunk_size=16)
    world.set_voxel(-0.05, -0.05, -0.05, 50000, LABEL_PERSON)
    world.set_voxel(0.05, 0.05, 0.05, 40000, LABEL_STRUCTURE)
    assert world.get_voxel(-0.05, -0.05, -0.05) == (50000, LABEL_PERSON)
    assert world.get_voxel(0.05, 0.05, 0.05) == (40000, LABEL_STRUCTURE)
    assert len(world.chunks) == 2, "truncation instead of floor would merge these"


def test_voxel_world_roundtrip_and_stats():
    world = VoxelWorld(voxel_size=0.1)
    rng = np.random.default_rng(14)
    points = rng.uniform(0, 10, (2000, 3))
    inserted = world.integrate_points(points, label=LABEL_STRUCTURE)
    assert inserted == 2000
    stats = world.stats()
    assert stats["occupied_voxels"] > 1500
    assert stats["compression_ratio"] > 5.0
    assert "structure" in stats["by_label"]


def test_point_cloud_respects_limits_and_labels():
    world = VoxelWorld(voxel_size=0.2)
    for i in range(50):
        world.set_voxel(i * 0.2, 0.0, 0.0, 40000, LABEL_STRUCTURE)
    for i in range(10):
        world.set_voxel(i * 0.2, 1.0, 0.0, 50000, LABEL_PERSON)
    assert len(world.point_cloud(max_points=20)) == 20
    people = world.point_cloud(labels={LABEL_PERSON})
    assert len(people) == 10
    assert all(p[4] == LABEL_PERSON for p in people)


def test_chunks_in_radius():
    world = VoxelWorld(voxel_size=0.5, chunk_size=8)   # 4 m chunks
    world.set_voxel(1.0, 1.0, 1.0, 40000)
    world.set_voxel(30.0, 30.0, 1.0, 40000)
    near = world.chunks_in_radius((1.0, 1.0, 1.0), 5.0)
    assert len(near) == 1


def test_voxel_prune():
    world = VoxelWorld()
    world.set_voxel(1.0, 1.0, 1.0, 40000, timestamp=100.0)
    world.set_voxel(2.0, 2.0, 2.0, 40000, timestamp=500.0)
    assert world.prune(older_than=300.0) == 1
    assert len(world.chunks) == 1


def test_octree_query_matches_brute_force():
    rng = np.random.default_rng(15)
    points = rng.uniform(0, 10, (3000, 3))
    tree = SparseVoxelOctree(center=(5, 5, 5), half_size=8.0)
    for point in points:
        tree.insert(*point)
    center = (5.0, 5.0, 5.0)
    radius = 1.5
    found = tree.query_sphere(center, radius)
    brute = [p for p in points if math.dist(p, center) <= radius]
    assert len(found) == len(brute)


def test_octree_rejects_out_of_bounds_and_subdivides():
    tree = SparseVoxelOctree(center=(0, 0, 0), half_size=1.0, bucket=4, max_depth=4)
    assert not tree.insert(100.0, 0.0, 0.0)
    for i in range(50):
        tree.insert(0.5 * math.cos(i), 0.5 * math.sin(i), 0.0)
    stats = tree.stats()
    assert stats["points"] == 50
    assert stats["nodes"] > 1, "the tree must have subdivided"


# ======================================================================
# audit chain
# ======================================================================
def test_canonical_json_is_order_independent():
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})


def test_chain_appends_and_verifies():
    validator = CausalValidator()
    for i in range(10):
        validator.append("ct45p", "scan.start", {"i": i})
    ok, index, message = validator.verify()
    assert ok and index is None
    assert "10 entries" in message
    assert validator.entries[0].prev_hash == "0" * 64
    assert validator.entries[5].prev_hash == validator.entries[4].chain_hash


def test_tampering_is_detected_at_the_right_index():
    validator = CausalValidator()
    for i in range(8):
        validator.append("ct45p", "sensor.read", {"value": i})
    validator.entries[3].payload["value"] = 999
    ok, index, message = validator.verify()
    assert not ok
    assert index == 3
    assert "modified" in message


def test_deleting_an_entry_breaks_the_chain():
    validator = CausalValidator()
    for i in range(6):
        validator.append("ct45p", "event", {"i": i})
    del validator.entries[2]
    ok, index, _ = validator.verify()
    assert not ok
    assert index == 2


def test_reordering_is_detected():
    validator = CausalValidator()
    for i in range(5):
        validator.append("ct45p", "event", {"i": i})
    validator.entries[1], validator.entries[2] = validator.entries[2], validator.entries[1]
    assert not validator.verify()[0]


def test_hash_is_reproducible_across_reload():
    validator = CausalValidator()
    for i in range(5):
        validator.append("ct45p", "event", {"i": i}, timestamp=1000.0 + i)
    exported = validator.export()
    reloaded = CausalValidator.load(exported)
    assert reloaded.verify()[0]
    assert reloaded.last_hash == validator.last_hash


def test_hmac_chain_cannot_be_forged_without_the_key():
    validator = CausalValidator(hmac_key=b"super-secret")
    for i in range(4):
        validator.append("ct45p", "event", {"i": i})
    assert validator.verify()[0]

    # an attacker rewrites an entry and recomputes the digest without the key
    forger = CausalValidator()
    validator.entries[2].payload["i"] = 42
    validator.entries[2].chain_hash = forger._digest(
        validator.entries[2].prev_hash, validator.entries[2].hashable()
    )
    assert not validator.verify()[0]


def test_severity_filtering_and_stats():
    validator = CausalValidator()
    validator.append("a", "x", severity="security")
    validator.append("b", "y", severity="info")
    validator.append("a", "z", severity="security")
    assert len(validator.tail(severity="security")) == 2
    stats = validator.stats()
    assert stats["count"] == 3
    assert stats["by_actor"]["a"] == 2
    assert stats["valid"]
    assert validator.append("c", "w", severity="nonsense").severity == "info"


def test_audit_store_keeps_every_concurrent_append(tmp_path):
    """200 threaded appends must all land, in memory and on disk.

    Without a lock around the SQLite write this lost 112 of 200 in memory
    and 131 on disk (SystemError / OperationalError('not an error')). The
    in-memory CausalValidator alone is fine — the loss is the connection.
    Dropping ``lock=store.lock`` fails this test.
    """
    import threading

    from aura.storage import LocalVectorStore

    store = LocalVectorStore(tmp_path / "audit-race.db", "race")
    audit = AuditStore(store.connection, lock=store.lock)
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        try:
            for i in range(20):
                audit.append("t", "act", {"n": n, "i": i})
        except Exception as exc:  # noqa: BLE001 - we want every failure
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == [], f"append raised under concurrency: {errors[:3]}"
    assert len(audit.validator.entries) == 200
    ok, index, message = audit.verify()
    assert ok, message
    assert index is None
    db_count = store.connection.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    assert db_count == 200, f"disk lost {200 - db_count} of 200 entries"


def test_audit_survives_concurrent_transform_writes(tmp_path):
    """The collision the running agent actually hits.

    Fusion writes transforms on the same connection the REST handler uses
    for the audit chain. A private lock on AuditStore does not exclude
    that writer. Measured without the shared lock: 5 of 200 audit entries
    survived and POST /audit returned 500.
    """
    import threading

    from aura.storage import LocalVectorStore

    store = LocalVectorStore(tmp_path / "shared.db", "shared")
    audit = AuditStore(store.connection, lock=store.lock)
    errors: list[tuple[str, str]] = []

    def write_transforms() -> None:
        try:
            for i in range(80):
                store.save_transform(
                    {"offset_x": i, "offset_y": 0, "offset_z": 0,
                     "roll": 0, "pitch": 0, "yaw": 0}
                )
        except Exception as exc:  # noqa: BLE001
            errors.append(("transform", repr(exc)))

    def write_audit() -> None:
        try:
            for i in range(80):
                audit.append("t", "act", {"i": i})
        except Exception as exc:  # noqa: BLE001
            errors.append(("audit", repr(exc)))

    threads = [
        threading.Thread(target=write_transforms),
        threading.Thread(target=write_transforms),
        threading.Thread(target=write_audit),
        threading.Thread(target=write_audit),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == [], f"shared-connection writes failed: {errors[:4]}"
    assert len(audit.validator.entries) == 160
    assert audit.verify()[0]
    db_count = store.connection.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    assert db_count == 160


def test_audit_store_persists_and_restores(tmp_path):
    path = tmp_path / "audit.db"
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    store = AuditStore(conn)
    for i in range(5):
        store.append("ct45p", "boot", {"i": i}, severity="notice")
    assert store.verify()[0]
    conn.close()

    conn2 = sqlite3.connect(str(path))
    conn2.row_factory = sqlite3.Row
    restored = AuditStore(conn2)
    ok, _, _ = restored.verify()
    assert ok
    assert restored.stats()["count"] == 5
    # the chain must continue seamlessly after a restart
    restored.append("ct45p", "shutdown", {})
    assert restored.verify()[0]
    conn2.close()
