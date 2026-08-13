"""Sensor drivers for the Aura edge agent.

Each driver exposes the same tiny contract::

    driver.open()      -> bool     acquire the hardware (never raises)
    driver.read()      -> payload  latest sample (None when nothing new)
    driver.close()     -> None
    driver.info        -> dict     status for /api/v1/agent/state

Every driver has a hardware implementation (pyserial / BlueZ) and a
synthetic twin used when ``AURA_SIMULATE=1`` or the hardware is absent, so
the whole stack runs end-to-end on a laptop or in CI.
"""

from .base import SensorDriver, SensorStatus
from .lidar import LidarDriver, LidarScan
from .mmwave import MmwaveDriver, MmwaveTarget
from .uwb import UwbDriver, UwbReading
from .ble import BleDriver, BleBeacon
from .imu import ImuDriver, ImuSample
from .thermal import ThermalDriver, ThermalFrame

__all__ = [
    "SensorDriver",
    "SensorStatus",
    "LidarDriver",
    "LidarScan",
    "MmwaveDriver",
    "MmwaveTarget",
    "UwbDriver",
    "UwbReading",
    "BleDriver",
    "BleBeacon",
    "ImuDriver",
    "ImuSample",
    "ThermalDriver",
    "ThermalFrame",
]
