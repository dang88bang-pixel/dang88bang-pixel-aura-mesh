"""Tamper-evident audit logging (IEC 62443 / IEC 62541-14 flavoured).

Every security- or safety-relevant action is appended to a hash chain::

    h_n = SHA256( h_{n-1} || canonical_json(entry) )

Any modification, reordering or deletion of a past entry changes every
subsequent hash, so :meth:`CausalValidator.verify` localises the exact index
where the log was tampered with.

Two deliberate design choices, both learned from getting them wrong:

* The chain hash covers a **canonical JSON serialisation** with sorted keys.
  Hashing ``str(dict)`` would make verification depend on Python's dict
  ordering and would not survive a round-trip through SQLite or the network.
* The timestamp is **inside** the hashed payload, not appended at hash time.
  A validator that mixes ``time.time()`` into the digest can never reproduce
  the hash later, which makes the whole chain unverifiable.

An optional HMAC key turns the chain into a MAC chain, so an attacker who can
rewrite the store still cannot forge a valid continuation without the key.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

GENESIS_HASH = "0" * 64


def canonical_json(payload: Any) -> str:
    """Deterministic JSON: sorted keys, no incidental whitespace."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


@dataclass
class AuditEntry:
    """One immutable record in the chain."""

    index: int
    timestamp: float
    actor: str
    action: str
    payload: dict[str, Any] = field(default_factory=dict)
    severity: str = "info"
    entry_id: str = ""
    prev_hash: str = GENESIS_HASH
    chain_hash: str = ""

    def hashable(self) -> str:
        """Exactly the bytes that go into the digest (hash fields excluded)."""
        return canonical_json(
            {
                "index": self.index,
                "timestamp": round(self.timestamp, 6),
                "actor": self.actor,
                "action": self.action,
                "payload": self.payload,
                "severity": self.severity,
                "entry_id": self.entry_id,
            }
        )

    def as_dict(self) -> dict:
        return asdict(self)


class CausalValidator:
    """Append-only hash chain with verification and export/import."""

    SEVERITIES = ("debug", "info", "notice", "warning", "critical", "security")

    def __init__(self, hmac_key: bytes | None = None, genesis: str = GENESIS_HASH) -> None:
        self.hmac_key = hmac_key
        self.genesis = genesis
        self.last_hash = genesis
        self.entries: list[AuditEntry] = []
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    def _digest(self, prev_hash: str, body: str) -> str:
        message = f"{prev_hash}|{body}".encode("utf-8")
        if self.hmac_key:
            return hmac.new(self.hmac_key, message, hashlib.sha256).hexdigest()
        return hashlib.sha256(message).hexdigest()

    def append(
        self,
        actor: str,
        action: str,
        payload: dict | None = None,
        severity: str = "info",
        timestamp: float | None = None,
    ) -> AuditEntry:
        """Append an entry and seal it into the chain."""
        if severity not in self.SEVERITIES:
            severity = "info"
        with self._lock:
            entry = AuditEntry(
                index=len(self.entries),
                timestamp=timestamp if timestamp is not None else time.time(),
                actor=actor,
                action=action,
                payload=dict(payload or {}),
                severity=severity,
                entry_id=f"aud-{uuid.uuid4().hex[:16]}",
                prev_hash=self.last_hash,
            )
            entry.chain_hash = self._digest(entry.prev_hash, entry.hashable())
            self.entries.append(entry)
            self.last_hash = entry.chain_hash
            return entry

    # ------------------------------------------------------------------
    def verify(self, entries: Iterable[AuditEntry] | None = None) -> tuple[bool, int | None, str]:
        """Recompute the whole chain.

        Returns ``(ok, first_bad_index, message)``.
        """
        with self._lock:
            records = list(entries if entries is not None else self.entries)
        expected_prev = self.genesis
        for position, entry in enumerate(records):
            if entry.index != position:
                return (False, position, f"index mismatch at {position}: stored {entry.index}")
            if entry.prev_hash != expected_prev:
                return (False, position, f"broken link at {position}: prev_hash does not match")
            recomputed = self._digest(entry.prev_hash, entry.hashable())
            if not hmac.compare_digest(recomputed, entry.chain_hash):
                return (False, position, f"payload at {position} was modified")
            expected_prev = entry.chain_hash
        return (True, None, f"chain of {len(records)} entries is intact")

    @property
    def valid(self) -> bool:
        return self.verify()[0]

    # ------------------------------------------------------------------
    def export(self) -> dict:
        with self._lock:
            return {
                "genesis": self.genesis,
                "count": len(self.entries),
                "head": self.last_hash,
                "hmac": bool(self.hmac_key),
                "entries": [e.as_dict() for e in self.entries],
            }

    @classmethod
    def load(cls, data: dict, hmac_key: bytes | None = None) -> "CausalValidator":
        validator = cls(hmac_key=hmac_key, genesis=data.get("genesis", GENESIS_HASH))
        for raw in data.get("entries", []):
            validator.entries.append(AuditEntry(**raw))
        if validator.entries:
            validator.last_hash = validator.entries[-1].chain_hash
        return validator

    def tail(self, count: int = 50, severity: str | None = None) -> list[dict]:
        with self._lock:
            records = self.entries
            if severity:
                records = [e for e in records if e.severity == severity]
            return [e.as_dict() for e in records[-count:]]

    def stats(self) -> dict:
        with self._lock:
            by_severity: dict[str, int] = {}
            by_actor: dict[str, int] = {}
            snapshot = list(self.entries)
            head = self.last_hash
        for entry in snapshot:
            by_severity[entry.severity] = by_severity.get(entry.severity, 0) + 1
            by_actor[entry.actor] = by_actor.get(entry.actor, 0) + 1
        ok, bad_index, message = self.verify(snapshot)
        return {
            "count": len(snapshot),
            "head": head,
            "valid": ok,
            "first_bad_index": bad_index,
            "message": message,
            "by_severity": by_severity,
            "by_actor": by_actor,
            "hmac_protected": bool(self.hmac_key),
        }


# ----------------------------------------------------------------------
# persistence
# ----------------------------------------------------------------------
AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    idx         INTEGER PRIMARY KEY,
    entry_id    TEXT NOT NULL UNIQUE,
    timestamp   REAL NOT NULL,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    severity    TEXT NOT NULL,
    payload     TEXT NOT NULL,
    prev_hash   TEXT NOT NULL,
    chain_hash  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_audit_severity ON audit_log(severity);
"""


class AuditStore:
    """SQLite-backed audit chain (the IPSM-persisted log on the device).

    The connection is almost always the one owned by
    :class:`~aura.storage.LocalVectorStore`. SQLite will not serialise two
    threads talking to the same connection: without a shared lock, concurrent
    ``append`` calls raise ``SystemError`` / ``OperationalError`` and the
    inserts that did not crash simply never land. Measured: 200 threaded
    appends kept 88 in memory and 69 on disk; sharing the connection with the
    fusion loop's transform writer left 5 of 200. The REST handler surfaced
    that as a 500 on ``POST /api/v1/agent/audit``.

    Pass ``lock=store.lock``. A private lock on this object is not enough —
    it does not exclude the fusion thread, which is the collision that
    actually 500s.
    """

    def __init__(
        self,
        connection,
        validator: CausalValidator | None = None,
        lock: threading.RLock | None = None,
    ) -> None:
        self.conn = connection
        self.validator = validator or CausalValidator()
        # Own lock only when the caller is not sharing the connection. The
        # production path (api.py) must pass LocalVectorStore.lock.
        self._lock = lock or threading.RLock()
        with self._lock:
            self.conn.executescript(AUDIT_SCHEMA)
            self.conn.commit()
            self._restore()

    def _restore(self) -> None:
        rows = self.conn.execute("SELECT * FROM audit_log ORDER BY idx ASC").fetchall()
        for row in rows:
            self.validator.entries.append(
                AuditEntry(
                    index=row["idx"],
                    timestamp=row["timestamp"],
                    actor=row["actor"],
                    action=row["action"],
                    payload=json.loads(row["payload"]),
                    severity=row["severity"],
                    entry_id=row["entry_id"],
                    prev_hash=row["prev_hash"],
                    chain_hash=row["chain_hash"],
                )
            )
        if self.validator.entries:
            self.validator.last_hash = self.validator.entries[-1].chain_hash

    def append(self, actor: str, action: str, payload: dict | None = None,
               severity: str = "info") -> AuditEntry:
        # Seal the chain *and* persist under the same lock. Splitting them
        # lets a crash between the two leave an in-memory entry that is not
        # on disk, so a restart silently shortens the chain.
        with self._lock:
            entry = self.validator.append(actor, action, payload, severity)
            self.conn.execute(
                "INSERT INTO audit_log (idx, entry_id, timestamp, actor, action, severity, payload, prev_hash, chain_hash)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    entry.index,
                    entry.entry_id,
                    entry.timestamp,
                    entry.actor,
                    entry.action,
                    entry.severity,
                    canonical_json(entry.payload),
                    entry.prev_hash,
                    entry.chain_hash,
                ),
            )
            self.conn.commit()
            return entry

    def verify(self) -> tuple[bool, int | None, str]:
        return self.validator.verify()

    def tail(self, count: int = 50, severity: str | None = None) -> list[dict]:
        return self.validator.tail(count, severity)

    def stats(self) -> dict:
        return self.validator.stats()
