#!/usr/bin/env python3
"""Generate the cross-platform audit digests used by the Kotlin test suite.

The Kotlin implementation (`CausalValidator.kt`) and the Python one
(`aura/audit.py`) must produce identical hashes, otherwise a chain written on a
CT45P cannot be verified on the edge agent. This script emits the Python-side
digests for fixed inputs; `tools/run-kotlin-tests.sh` substitutes them into
`AuditChainTest.kt` before compiling.

Run from the repository root:
    python3 tools/generate_audit_fixtures.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "edge-agent"))

from aura.audit import GENESIS_HASH, CausalValidator  # noqa: E402

# The exact canonical bodies the Kotlin test builds. Keep in sync with
# AuditChainTest.crossPlatformHashMatchesPython.
BODY_0 = (
    '{"action":"scan.start","actor":"ct45p","entry_id":"aud-0000000000000001",'
    '"index":0,"payload":{"i":0},"severity":"info","timestamp":1000.0}'
)
BODY_1 = (
    '{"action":"marker.place","actor":"operator","entry_id":"aud-0000000000000002",'
    '"index":1,"payload":{"x":3.5,"y":4.0},"severity":"security","timestamp":1000.5}'
)


def main() -> int:
    validator = CausalValidator()
    digest_0 = validator._digest(GENESIS_HASH, BODY_0)
    digest_1 = validator._digest(digest_0, BODY_1)

    print(f"DIGEST0={digest_0}")
    print(f"DIGEST1={digest_1}")

    # Sanity: the Python side must also round-trip its own canonical form.
    entry = validator.append(
        "ct45p", "scan.start", {"i": 0}, severity="info", timestamp=1000.0
    )
    entry.entry_id = "aud-0000000000000001"
    entry.index = 0
    rebuilt = entry.hashable()
    if rebuilt != BODY_0:
        print("WARNING: Python canonical body drifted from the fixture:", file=sys.stderr)
        print(f"  fixture: {BODY_0}", file=sys.stderr)
        print(f"  actual : {rebuilt}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
