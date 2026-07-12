"""Normalize libdyson device state into flat scalar JSON for NATS consumers.

The knx-nats-bridge writer can only extract named scalar fields (no arrays,
no transforms), so all Dyson dialect quirks are resolved here: ON/OFF strings
become booleans, fan speed AUTO becomes 0 (matching the KNX GA semantics
"Stufe 0 = Automatik"), Kelvin becomes Celsius, and sensor sleep sentinels
(negative values / None) drop the field entirely.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# libdyson reports environmental sentinels as negative ints
# (ENVIRONMENTAL_OFF=-1, ENVIRONMENTAL_INIT=-2, ENVIRONMENTAL_FAIL=-3).
_KELVIN_OFFSET = 273.15


class FanState(Protocol):
    """The subset of libdyson's DysonPureCool surface we read."""

    @property
    def is_on(self) -> bool: ...
    @property
    def auto_mode(self) -> bool: ...
    @property
    def speed(self) -> int | None: ...
    @property
    def oscillation(self) -> bool: ...
    @property
    def night_mode(self) -> bool: ...
    @property
    def temperature(self) -> float: ...
    @property
    def humidity(self) -> int: ...
    @property
    def particulate_matter_2_5(self) -> int: ...
    @property
    def particulate_matter_10(self) -> int: ...
    @property
    def volatile_organic_compounds(self) -> float: ...
    @property
    def nitrogen_dioxide(self) -> float: ...


def _read(device: FanState, attr: str) -> Any:
    """Read one device property, returning None when the field is unavailable.

    libdyson raises (KeyError/ValueError/TypeError) when a status field is
    missing or non-numeric, e.g. right after connect before the first
    CURRENT-STATE arrived.
    """
    try:
        return getattr(device, attr)
    except Exception as exc:
        logger.debug("device field %s unavailable: %s", attr, exc)
        return None


def normalize_state(device: FanState) -> dict[str, Any]:
    """Flat control-state payload; `speed` 0 encodes auto mode."""
    state: dict[str, Any] = {}
    for key, attr in (
        ("power", "is_on"),
        ("auto", "auto_mode"),
        ("oscillation", "oscillation"),
        ("night", "night_mode"),
    ):
        value = _read(device, attr)
        if value is not None:
            state[key] = bool(value)

    if state.get("auto"):
        state["speed"] = 0
    else:
        speed = _read(device, "speed")
        if speed is not None:
            state["speed"] = int(speed)
    return state


def normalize_environment(device: FanState) -> dict[str, Any]:
    """Flat sensor payload; sleeping/failed sensors (sentinel < 0, None) are omitted."""
    environment: dict[str, Any] = {}

    temperature = _read(device, "temperature")
    if temperature is not None and temperature > 0:
        environment["temperature_c"] = round(temperature - _KELVIN_OFFSET, 1)

    for key, attr in (
        ("humidity", "humidity"),
        ("pm25", "particulate_matter_2_5"),
        ("pm10", "particulate_matter_10"),
        ("voc", "volatile_organic_compounds"),
        ("no2", "nitrogen_dioxide"),
    ):
        value = _read(device, attr)
        if value is not None and value >= 0:
            environment[key] = value
    return environment
