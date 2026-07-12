"""Unit tests for command parsing and dispatch."""

from __future__ import annotations

import json
from typing import Any

import pytest

from dyson_nats_bridge.commands import CommandHandler, parse_command
from dyson_nats_bridge.config import Settings
from dyson_nats_bridge.metrics import Metrics


def _payload(value: Any) -> bytes:
    return json.dumps({"value": value}).encode()


def test_parse_switch_bool_and_numeric() -> None:
    assert parse_command("dyson.x.command.power", _payload(True)) == ("power", True)
    assert parse_command("dyson.x.command.night", _payload(0)) == ("night", False)


def test_parse_oscillation_mode_enum() -> None:
    # 0 = off, 1..4 = 45/90/180/350 degrees; bools = on (last angle) / off.
    assert parse_command("dyson.x.command.oscillation", _payload(0)) == ("oscillation", 0)
    assert parse_command("dyson.x.command.oscillation", _payload(4)) == ("oscillation", 4)
    assert parse_command("dyson.x.command.oscillation", _payload(True)) == ("oscillation", True)
    with pytest.raises(ValueError, match="oscillation must be"):
        parse_command("dyson.x.command.oscillation", _payload(5))


def test_parse_speed_range() -> None:
    assert parse_command("dyson.x.command.speed", _payload(0)) == ("speed", 0)
    assert parse_command("dyson.x.command.speed", _payload(10)) == ("speed", 10)
    with pytest.raises(ValueError, match="0..10"):
        parse_command("dyson.x.command.speed", _payload(11))
    with pytest.raises(ValueError, match="must be a number"):
        parse_command("dyson.x.command.speed", _payload(True))


def test_parse_rejects_unknown_function() -> None:
    with pytest.raises(ValueError, match="unknown command function"):
        parse_command("dyson.x.command.warp", _payload(1))


def test_parse_rejects_bad_payloads() -> None:
    with pytest.raises(ValueError, match="invalid JSON"):
        parse_command("dyson.x.command.power", b"{nope")
    with pytest.raises(ValueError, match='"value" field'):
        parse_command("dyson.x.command.power", b'{"on": true}')
    with pytest.raises(ValueError, match="must be a boolean"):
        parse_command("dyson.x.command.power", _payload(3))


class FakeMsg:
    def __init__(self, subject: str, data: bytes) -> None:
        self.subject = subject
        self.data = data


class FakeBridge:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.applied: list[tuple[str, Any]] = []

    def apply_command(self, function: str, value: Any) -> None:
        if self.fail:
            raise RuntimeError("device offline")
        self.applied.append((function, value))


def _settings() -> Settings:
    return Settings(
        dyson_host="fan.local", dyson_serial="XX1-EU-ABC1234A", dyson_device_name="testraum"
    )


def _counter_value(metrics: Metrics, function: str, outcome: str) -> float:
    return metrics.commands.labels(function=function, outcome=outcome)._value.get()


async def test_handler_applies_valid_command() -> None:
    metrics = Metrics()
    bridge = FakeBridge()
    handler = CommandHandler(_settings(), bridge, publisher=None, metrics=metrics)  # type: ignore[arg-type]
    await handler._on_command(FakeMsg("dyson.testraum.command.speed", _payload(7)))  # type: ignore[arg-type]
    assert bridge.applied == [("speed", 7)]
    assert _counter_value(metrics, "speed", "ok") == 1


async def test_handler_counts_invalid_without_applying() -> None:
    metrics = Metrics()
    bridge = FakeBridge()
    handler = CommandHandler(_settings(), bridge, publisher=None, metrics=metrics)  # type: ignore[arg-type]
    await handler._on_command(FakeMsg("dyson.testraum.command.speed", _payload(99)))  # type: ignore[arg-type]
    assert bridge.applied == []
    assert _counter_value(metrics, "speed", "invalid") == 1


async def test_handler_counts_device_errors() -> None:
    metrics = Metrics()
    bridge = FakeBridge(fail=True)
    handler = CommandHandler(_settings(), bridge, publisher=None, metrics=metrics)  # type: ignore[arg-type]
    await handler._on_command(FakeMsg("dyson.testraum.command.power", _payload(True)))  # type: ignore[arg-type]
    assert _counter_value(metrics, "power", "error") == 1
