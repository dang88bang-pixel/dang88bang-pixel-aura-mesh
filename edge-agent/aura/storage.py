"""Persistent context store: SQLite in WAL mode.

Mirrors ``LocalVectorStore.kt`` on the Android side so a CT45P can sync its
recordings up to the agent (and back) without any schema translation.

Tables
------
``projects``     one row per survey/site
``transforms``   EKF pose history (the "3D transform" records)
``events``       generic sensor events (BLE, UWB, mmWave, thermal)
``maps``         serialised occupancy grids per project version
``scenarios``    scenario runs and their results
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS projects (
    project_id   TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL,
    version      INTEGER NOT NULL DEFAULT 1,
    metadata     TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS transforms (
    record_id    TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL,
    timestamp    REAL NOT NULL,
    offset_x     REAL NOT NULL,
    offset_y     REAL NOT NULL,
    offset_z     REAL NOT NULL,
    roll         REAL NOT NULL,
    pitch        REAL NOT NULL,
    yaw          REAL NOT NULL,
    velocity     TEXT NOT NULL DEFAULT '[0,0,0]',
    covariance   TEXT NOT NULL DEFAULT '[]',
    metadata     TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_transforms_project_ts ON transforms(project_id, timestamp DESC);

CREATE TABLE IF NOT EXISTS events (
    event_id     TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL,
    timestamp    REAL NOT NULL,
    source       TEXT NOT NULL,
    payload      TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_events_project_ts ON events(project_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_events_source ON events(source);

CREATE TABLE IF NOT EXISTS maps (
    map_id       TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL,
    timestamp    REAL NOT NULL,
    version      INTEGER NOT NULL,
    resolution   REAL NOT NULL,
    origin_x     REAL NOT NULL,
    origin_y     REAL NOT NULL,
    cells        INTEGER NOT NULL,
    grid         BLOB NOT NULL,
    stats        TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_maps_project ON maps(project_id, version DESC);

CREATE TABLE IF NOT EXISTS scenarios (
    run_id       TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL,
    scenario     TEXT NOT NULL,
    started_at   REAL NOT NULL,
    finished_at  REAL,
    params       TEXT NOT NULL DEFAULT '{}',
    result       TEXT NOT NULL DEFAULT '{}',
    status       TEXT NOT NULL DEFAULT 'running',
    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_scenarios_project ON scenarios(project_id, started_at DESC);
"""


@dataclass
class TransformRecord:
    record_id: str
    project_id: str
    timestamp: float
    offset_x: float
    offset_y: float
    offset_z: float
    roll: float
    pitch: float
    yaw: float
    velocity: list[float]
    covariance: list[float]
    metadata: dict

    def as_dict(self) -> dict:
        return {
            "record_id": self.record_id,
            "project_id": self.project_id,
            "timestamp": self.timestamp,
            "transform": {
                "offset_x": self.offset_x,
                "offset_y": self.offset_y,
                "offset_z": self.offset_z,
                "roll": self.roll,
                "pitch": self.pitch,
                "yaw": self.yaw,
            },
            "velocity": self.velocity,
            "covariance": self.covariance,
            "metadata": self.metadata,
        }


class LocalVectorStore:
    """Thread-safe SQLite context store (WAL, retention-managed)."""

    def __init__(self, path: str | Path, project: str = "default") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()
        self.project_id = self.ensure_project(project)
        self.writes = 0

    # ------------------------------------------------------------------
    @property
    def connection(self):
        """The underlying SQLite connection.

        Exposed so collaborators sharing this database (the audit store) need
        not reach for a private attribute. Deliberately read-only: the store
        still owns the connection's lifetime and closes it.
        """
        return self._conn

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.commit()
            finally:
                self._conn.close()

    def ensure_project(self, name: str, metadata: dict | None = None) -> str:
        now = time.time()
        with self._lock:
            row = self._conn.execute("SELECT project_id FROM projects WHERE name = ?", (name,)).fetchone()
            if row:
                self._conn.execute("UPDATE projects SET updated_at = ? WHERE project_id = ?", (now, row["project_id"]))
                self._conn.commit()
                return str(row["project_id"])
            project_id = f"prj-{uuid.uuid4().hex[:12]}"
            self._conn.execute(
                "INSERT INTO projects (project_id, name, created_at, updated_at, version, metadata) VALUES (?,?,?,?,?,?)",
                (project_id, name, now, now, 1, json.dumps(metadata or {})),
            )
            self._conn.commit()
            return project_id

    def list_projects(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT p.*, (SELECT COUNT(*) FROM transforms t WHERE t.project_id = p.project_id) AS transform_count"
                " FROM projects p ORDER BY updated_at DESC"
            ).fetchall()
        return [
            {
                "project_id": r["project_id"],
                "name": r["name"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "version": r["version"],
                "transform_count": r["transform_count"],
                "metadata": json.loads(r["metadata"] or "{}"),
            }
            for r in rows
        ]

    def bump_version(self, project_id: str | None = None) -> int:
        pid = project_id or self.project_id
        with self._lock:
            self._conn.execute(
                "UPDATE projects SET version = version + 1, updated_at = ? WHERE project_id = ?", (time.time(), pid)
            )
            self._conn.commit()
            row = self._conn.execute("SELECT version FROM projects WHERE project_id = ?", (pid,)).fetchone()
        return int(row["version"]) if row else 1

    # ------------------------------------------------------------------
    def save_transform(
        self,
        transform: dict,
        covariance: Iterable[float] = (),
        metadata: dict | None = None,
        velocity: Iterable[float] = (),
        timestamp: float | None = None,
        project_id: str | None = None,
    ) -> str:
        record_id = f"trf-{uuid.uuid4().hex[:14]}"
        ts = timestamp if timestamp is not None else time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO transforms (record_id, project_id, timestamp, offset_x, offset_y, offset_z,"
                " roll, pitch, yaw, velocity, covariance, metadata) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record_id,
                    project_id or self.project_id,
                    ts,
                    float(transform.get("offset_x", 0.0)),
                    float(transform.get("offset_y", 0.0)),
                    float(transform.get("offset_z", 0.0)),
                    float(transform.get("roll", 0.0)),
                    float(transform.get("pitch", 0.0)),
                    float(transform.get("yaw", 0.0)),
                    json.dumps([round(float(v), 5) for v in velocity]),
                    json.dumps([round(float(v), 8) for v in covariance]),
                    json.dumps(metadata or {}),
                ),
            )
            self.writes += 1
            self._conn.commit()
        return record_id

    def history(self, limit: int = 500, project_id: str | None = None, since: float | None = None) -> list[dict]:
        pid = project_id or self.project_id
        query = "SELECT * FROM transforms WHERE project_id = ?"
        params: list[Any] = [pid]
        if since is not None:
            query += " AND timestamp >= ?"
            params.append(since)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        records = [
            TransformRecord(
                record_id=r["record_id"],
                project_id=r["project_id"],
                timestamp=r["timestamp"],
                offset_x=r["offset_x"],
                offset_y=r["offset_y"],
                offset_z=r["offset_z"],
                roll=r["roll"],
                pitch=r["pitch"],
                yaw=r["yaw"],
                velocity=json.loads(r["velocity"] or "[]"),
                covariance=json.loads(r["covariance"] or "[]"),
                metadata=json.loads(r["metadata"] or "{}"),
            ).as_dict()
            for r in rows
        ]
        records.reverse()
        return records

    # ------------------------------------------------------------------
    def save_event(self, source: str, payload: dict, timestamp: float | None = None,
                   project_id: str | None = None) -> str:
        event_id = f"evt-{uuid.uuid4().hex[:14]}"
        with self._lock:
            self._conn.execute(
                "INSERT INTO events (event_id, project_id, timestamp, source, payload) VALUES (?,?,?,?,?)",
                (event_id, project_id or self.project_id, timestamp or time.time(), source, json.dumps(payload)),
            )
            self.writes += 1
            self._conn.commit()
        return event_id

    def events(self, source: str | None = None, limit: int = 200, project_id: str | None = None) -> list[dict]:
        pid = project_id or self.project_id
        query = "SELECT * FROM events WHERE project_id = ?"
        params: list[Any] = [pid]
        if source:
            query += " AND source = ?"
            params.append(source)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [
            {
                "event_id": r["event_id"],
                "timestamp": r["timestamp"],
                "source": r["source"],
                "payload": json.loads(r["payload"]),
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    def save_map(self, grid_bytes: bytes, resolution: float, origin: tuple[float, float], cells: int,
                 stats: dict | None = None, project_id: str | None = None) -> str:
        pid = project_id or self.project_id
        version = self.bump_version(pid)
        map_id = f"map-{uuid.uuid4().hex[:12]}"
        with self._lock:
            self._conn.execute(
                "INSERT INTO maps (map_id, project_id, timestamp, version, resolution, origin_x, origin_y, cells, grid, stats)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (map_id, pid, time.time(), version, resolution, origin[0], origin[1], cells,
                 sqlite3.Binary(grid_bytes), json.dumps(stats or {})),
            )
            self._conn.commit()
        return map_id

    def load_map(self, project_id: str | None = None, version: int | None = None) -> dict | None:
        pid = project_id or self.project_id
        query = "SELECT * FROM maps WHERE project_id = ?"
        params: list[Any] = [pid]
        if version is not None:
            query += " AND version = ?"
            params.append(version)
        query += " ORDER BY version DESC LIMIT 1"
        with self._lock:
            row = self._conn.execute(query, params).fetchone()
        if not row:
            return None
        return {
            "map_id": row["map_id"],
            "timestamp": row["timestamp"],
            "version": row["version"],
            "resolution": row["resolution"],
            "origin": [row["origin_x"], row["origin_y"]],
            "cells": row["cells"],
            "grid": bytes(row["grid"]),
            "stats": json.loads(row["stats"] or "{}"),
        }

    def map_versions(self, project_id: str | None = None) -> list[dict]:
        pid = project_id or self.project_id
        with self._lock:
            rows = self._conn.execute(
                "SELECT map_id, timestamp, version, stats FROM maps WHERE project_id = ? ORDER BY version DESC", (pid,)
            ).fetchall()
        return [
            {"map_id": r["map_id"], "timestamp": r["timestamp"], "version": r["version"],
             "stats": json.loads(r["stats"] or "{}")}
            for r in rows
        ]

    # ------------------------------------------------------------------
    def start_scenario(self, scenario: str, params: dict, project_id: str | None = None) -> str:
        run_id = f"run-{uuid.uuid4().hex[:12]}"
        with self._lock:
            self._conn.execute(
                "INSERT INTO scenarios (run_id, project_id, scenario, started_at, params, status)"
                " VALUES (?,?,?,?,?,'running')",
                (run_id, project_id or self.project_id, scenario, time.time(), json.dumps(params)),
            )
            self._conn.commit()
        return run_id

    def finish_scenario(self, run_id: str, result: dict, status: str = "completed") -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE scenarios SET finished_at = ?, result = ?, status = ? WHERE run_id = ?",
                (time.time(), json.dumps(result), status, run_id),
            )
            self._conn.commit()

    def scenario_runs(self, limit: int = 50, project_id: str | None = None) -> list[dict]:
        pid = project_id or self.project_id
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM scenarios WHERE project_id = ? ORDER BY started_at DESC LIMIT ?", (pid, int(limit))
            ).fetchall()
        return [
            {
                "run_id": r["run_id"],
                "scenario": r["scenario"],
                "started_at": r["started_at"],
                "finished_at": r["finished_at"],
                "params": json.loads(r["params"] or "{}"),
                "result": json.loads(r["result"] or "{}"),
                "status": r["status"],
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    def enforce_retention(self, max_age_seconds: float, max_records: int) -> dict:
        """Delete old/overflowing rows; returns how many were removed."""
        cutoff = time.time() - max_age_seconds
        removed = {"transforms": 0, "events": 0}
        with self._lock:
            cur = self._conn.execute("DELETE FROM transforms WHERE timestamp < ?", (cutoff,))
            removed["transforms"] += cur.rowcount or 0
            cur = self._conn.execute("DELETE FROM events WHERE timestamp < ?", (cutoff,))
            removed["events"] += cur.rowcount or 0
            cur = self._conn.execute(
                "DELETE FROM transforms WHERE record_id IN ("
                "  SELECT record_id FROM transforms ORDER BY timestamp DESC LIMIT -1 OFFSET ?)",
                (int(max_records),),
            )
            removed["transforms"] += cur.rowcount or 0
            cur = self._conn.execute(
                "DELETE FROM events WHERE event_id IN ("
                "  SELECT event_id FROM events ORDER BY timestamp DESC LIMIT -1 OFFSET ?)",
                (int(max_records),),
            )
            removed["events"] += cur.rowcount or 0
            self._conn.commit()
        return removed

    def stats(self) -> dict:
        with self._lock:
            counts = {
                table: int(self._conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"])
                for table in ("projects", "transforms", "events", "maps", "scenarios")
            }
            page_size = int(self._conn.execute("PRAGMA page_size").fetchone()[0])
            page_count = int(self._conn.execute("PRAGMA page_count").fetchone()[0])
            journal = str(self._conn.execute("PRAGMA journal_mode").fetchone()[0])
        return {
            **counts,
            "db_path": str(self.path),
            "size_bytes": page_size * page_count,
            "journal_mode": journal,
            "writes": self.writes,
        }

    def vacuum(self) -> None:
        with self._lock:
            self._conn.execute("VACUUM")
            self._conn.commit()
