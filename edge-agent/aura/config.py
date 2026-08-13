"""Runtime configuration for the Aura edge agent.

Every value can be overridden with an ``AURA_``-prefixed environment
variable, which keeps the Docker deployment declarative.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any


def _env(name: str, default: Any) -> Any:
    raw = os.environ.get(f"AURA_{name.upper()}")
    if raw is None:
        return default
    if isinstance(default, bool):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(default, int) and not isinstance(default, bool):
        try:
            return int(raw)
        except ValueError:
            return default
    if isinstance(default, float):
        try:
            return float(raw)
        except ValueError:
            return default
    return raw


@dataclass
class SensorConfig:
    """Serial/radio wiring. Empty port + ``simulate`` -> synthetic driver."""

    lidar_port: str = field(default_factory=lambda: _env("LIDAR_PORT", ""))
    lidar_baud: int = field(default_factory=lambda: _env("LIDAR_BAUD", 115200))
    mmwave_port: str = field(default_factory=lambda: _env("MMWAVE_PORT", ""))
    mmwave_baud: int = field(default_factory=lambda: _env("MMWAVE_BAUD", 921600))
    mmwave_reduced: bool = field(default_factory=lambda: _env("MMWAVE_REDUCED", False))
    uwb_port: str = field(default_factory=lambda: _env("UWB_PORT", ""))
    uwb_baud: int = field(default_factory=lambda: _env("UWB_BAUD", 115200))
    imu_port: str = field(default_factory=lambda: _env("IMU_PORT", ""))
    ble_adapter: str = field(default_factory=lambda: _env("BLE_ADAPTER", "hci0"))


@dataclass
class AgentConfig:
    """Top-level agent configuration."""

    host: str = field(default_factory=lambda: _env("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _env("PORT", 8080))
    simulate: bool = field(default_factory=lambda: _env("SIMULATE", True))
    loop_hz: float = field(default_factory=lambda: _env("LOOP_HZ", 20.0))
    broadcast_hz: float = field(default_factory=lambda: _env("BROADCAST_HZ", 10.0))
    db_path: str = field(default_factory=lambda: _env("DB_PATH", "data/aura.db"))
    project: str = field(default_factory=lambda: _env("PROJECT", "default"))
    retention_seconds: int = field(default_factory=lambda: _env("RETENTION_SECONDS", 7 * 24 * 3600))
    retention_records: int = field(default_factory=lambda: _env("RETENTION_RECORDS", 250_000))
    persist_every: int = field(default_factory=lambda: _env("PERSIST_EVERY", 5))
    map_resolution: float = field(default_factory=lambda: _env("MAP_RESOLUTION", 0.10))
    map_size: float = field(default_factory=lambda: _env("MAP_SIZE", 48.0))
    cors_origins: str = field(default_factory=lambda: _env("CORS_ORIGINS", "*"))
    api_token: str = field(default_factory=lambda: _env("API_TOKEN", ""))
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))
    sensors: SensorConfig = field(default_factory=SensorConfig)

    def resolved_db_path(self) -> Path:
        path = Path(self.db_path).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def as_dict(self) -> dict:
        return asdict(self)

    def apply(self, patch: dict) -> list[str]:
        """Apply a partial config update. Returns the list of changed keys."""
        changed: list[str] = []
        valid = {f.name for f in fields(self)} - {"sensors"}
        for key, value in patch.items():
            if key == "sensors" and isinstance(value, dict):
                for skey, svalue in value.items():
                    if hasattr(self.sensors, skey):
                        setattr(self.sensors, skey, svalue)
                        changed.append(f"sensors.{skey}")
                continue
            if key in valid:
                current = getattr(self, key)
                try:
                    coerced = type(current)(value) if not isinstance(value, type(current)) else value
                except (TypeError, ValueError):
                    continue
                setattr(self, key, coerced)
                changed.append(key)
        return changed

    def __str__(self) -> str:  # pragma: no cover - debugging helper
        return json.dumps(self.as_dict(), indent=2)


CONFIG = AgentConfig()
