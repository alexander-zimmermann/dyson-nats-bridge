"""Unit tests for the Dyson-dialect normalization."""

from __future__ import annotations

from typing import Any

from dyson_nats_bridge.normalize import normalize_environment, normalize_state


class FakeDevice:
    """Property-bag stand-in for libdyson's DysonPureCool; raising attrs simulate
    fields that are missing before the first CURRENT-STATE arrived."""

    def __init__(self, **fields: Any) -> None:
        self._fields = fields

    def __getattr__(self, name: str) -> Any:
        if name in self._fields:
            value = self._fields[name]
            if isinstance(value, Exception):
                raise value
            return value
        raise KeyError(name)


def test_state_manual_speed() -> None:
    device = FakeDevice(is_on=True, auto_mode=False, speed=4, oscillation=True, night_mode=False)
    assert normalize_state(device) == {
        "power": True,
        "auto": False,
        "speed": 4,
        "oscillation": True,
        "night": False,
    }


def test_state_auto_mode_encodes_speed_zero() -> None:
    # libdyson returns speed=None while fnsp == "AUTO".
    device = FakeDevice(is_on=True, auto_mode=True, speed=None, oscillation=False, night_mode=True)
    state = normalize_state(device)
    assert state["speed"] == 0
    assert state["auto"] is True


def test_state_missing_fields_are_omitted() -> None:
    device = FakeDevice(
        is_on=True,
        auto_mode=False,
        speed=ValueError("fnsp not numeric"),
        oscillation=KeyError("oson"),
        night_mode=False,
    )
    state = normalize_state(device)
    assert "speed" not in state
    assert "oscillation" not in state
    assert state == {"power": True, "auto": False, "night": False}


def test_state_speed_none_without_auto_is_omitted() -> None:
    device = FakeDevice(
        is_on=False, auto_mode=False, speed=None, oscillation=False, night_mode=False
    )
    assert "speed" not in normalize_state(device)


def test_environment_kelvin_to_celsius() -> None:
    device = FakeDevice(
        temperature=294.65, humidity=52, particulate_matter_2_5=3,
        particulate_matter_10=5, volatile_organic_compounds=1.2, nitrogen_dioxide=0.4,
    )
    assert normalize_environment(device) == {
        "temperature_c": 21.5,
        "humidity": 52,
        "pm25": 3,
        "pm10": 5,
        "voc": 1.2,
        "no2": 0.4,
    }


def test_environment_sentinels_and_missing_are_omitted() -> None:
    # libdyson sentinels: OFF=-1, INIT=-2, FAIL=-3; NONE -> None.
    device = FakeDevice(
        temperature=-1, humidity=-2, particulate_matter_2_5=-3,
        particulate_matter_10=None, volatile_organic_compounds=KeyError("va10"),
        nitrogen_dioxide=0,
    )
    assert normalize_environment(device) == {"no2": 0}
