#!/usr/bin/env python3
"""Aura edge agent entry point.

Usage::

    python agent.py                       # simulated sensors, port 8080
    python agent.py --port 9000 --hardware
    AURA_SIMULATE=0 AURA_LIDAR_PORT=/dev/ttyUSB0 python agent.py
"""

from __future__ import annotations

import argparse
import logging
import sys

import uvicorn

from aura import __version__
from aura.api import create_app
from aura.config import CONFIG


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Aura edge agent")
    parser.add_argument("--host", default=None, help="bind address (default 0.0.0.0)")
    parser.add_argument("--port", type=int, default=None, help="bind port (default 8080)")
    parser.add_argument("--hardware", action="store_true", help="use real sensors instead of simulation")
    parser.add_argument("--simulate", action="store_true", help="force simulation mode")
    parser.add_argument("--db", default=None, help="SQLite path (default data/aura.db)")
    parser.add_argument("--project", default=None, help="project/site name")
    parser.add_argument("--loop-hz", type=float, default=None, help="fusion loop rate")
    parser.add_argument("--log-level", default=None, help="DEBUG/INFO/WARNING/ERROR")
    parser.add_argument("--version", action="store_true", help="print version and exit")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.version:
        print(f"aura-edge-agent {__version__}")
        return 0

    if args.host:
        CONFIG.host = args.host
    if args.port:
        CONFIG.port = args.port
    if args.hardware:
        CONFIG.simulate = False
    if args.simulate:
        CONFIG.simulate = True
    if args.db:
        CONFIG.db_path = args.db
    if args.project:
        CONFIG.project = args.project
    if args.loop_hz:
        CONFIG.loop_hz = args.loop_hz
    if args.log_level:
        CONFIG.log_level = args.log_level

    logging.basicConfig(
        level=getattr(logging, CONFIG.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )
    log = logging.getLogger("aura")
    log.info("Aura edge agent %s", __version__)
    log.info("mode=%s db=%s project=%s loop=%.1f Hz",
             "SIMULATION" if CONFIG.simulate else "HARDWARE",
             CONFIG.resolved_db_path(), CONFIG.project, CONFIG.loop_hz)

    app = create_app(CONFIG)
    uvicorn.run(app, host=CONFIG.host, port=CONFIG.port, log_level=CONFIG.log_level.lower(), access_log=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
