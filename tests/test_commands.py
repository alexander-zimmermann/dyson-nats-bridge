"""Unit tests for command parsing and per-device dispatch."""

from __future__ import annotations

import json
from typing import Any

import pytest

from dyson_nats_bridge.commands import CommandHandler, parse_command, split_subject
from dyson_nats_bridge.config import Settings
from dyson_nats_bridge.metrics import Metrics


def _payload(value: Any) -> bytes:
    return json.dumps({"value": value}).encode()


def test_split_subject() -> None:
    assert split_subject("dyson.ventilator-1.command.power") == ("ventilator-1", "power")
    for bad in ("dyson.x.power", "dyson.x.state.power", "dyson.x.command.power.extra"):
        with pytest.raises(ValueError, match="malformed command subject"):
            split_subject(bad)


def test_parse_switch_bool_and_numeric() -> None:
    assert parse_command("dyson.x.command.power", _payload(True)) == ("x", "power", True)
    assert parse_command("dyson.y.command.night", _payload(0)) == ("y", "night", False)


def test_parse_oscillation_mode_enum() -> None:
    # 0 = off, 1..4 = 45/90/180/350 degrees; bools = on (last angle) / off.
    assert parse_command("dyson.x.command.oscillation", _payload(0)) == ("x", "oscillation", 0)
    assert parse_command("dyson.x.command.oscillation", _payload(4)) == ("x", "oscillation", 4)
    assert parse_command("dyson.x.command.oscillation", _payload(True)) == (
        "x",
        "oscillation",
        True,
    )
    with pytest.raises(ValueError, match="oscillation must be"):
        parse_command("dyson.x.command.oscillation", _payload(5))


def test_parse_speed_range() -> None:
    assert parse_command("dyson.x.command.speed", _payload(0)) == ("x", "speed", 0)
    assert parse_command("dyson.x.command.speed", _payload(10)) == ("x", "speed", 10)
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
    def __init__(self, fail: bool = False, locked: bool = False) -> None:
        self.fail = fail
        self.locked = locked
        self.applied: list[tuple[str, Any]] = []

    def apply_command(self, function: str, value: Any) -> None:
        if self.fail:
            raise RuntimeError("device offline")
        self.applied.append((function, value))

    def set_lock(self, locked: bool) -> None:
        self.locked = locked


def _handler(
    metrics: Metrics, **bridges: FakeBridge
) -> tuple[CommandHandler, dict[str, FakeBridge]]:
    handler = CommandHandler(
        Settings(),
        bridges,  # type: ignore[arg-type]
        publisher=None,  # type: ignore[arg-type]
        metrics=metrics,
    )
    return handler, bridges


def _counter_value(metrics: Metrics, device: str, function: str, outcome: str) -> float:
    counter = metrics.commands.labels(device=device, function=function, outcome=outcome)
    return float(counter._value.get())


async def test_handler_routes_to_the_addressed_device() -> None:
    metrics = Metrics()
    handler, bridges = _handler(metrics, schlafzimmer=FakeBridge(), buero=FakeBridge())

    await handler._on_command(FakeMsg("dyson.buero.command.speed", _payload(7)))  # type: ignore[arg-type]

    assert bridges["buero"].applied == [("speed", 7)]
    assert bridges["schlafzimmer"].applied == []
    assert _counter_value(metrics, "buero", "speed", "ok") == 1


async def test_handler_counts_unknown_device_without_applying() -> None:
    metrics = Metrics()
    handler, bridges = _handler(metrics, schlafzimmer=FakeBridge())

    await handler._on_command(FakeMsg("dyson.kueche.command.power", _payload(True)))  # type: ignore[arg-type]

    assert bridges["schlafzimmer"].applied == []
    assert _counter_value(metrics, "kueche", "power", "unknown_device") == 1


async def test_handler_counts_invalid_without_applying() -> None:
    metrics = Metrics()
    handler, bridges = _handler(metrics, testraum=FakeBridge())

    await handler._on_command(FakeMsg("dyson.testraum.command.speed", _payload(99)))  # type: ignore[arg-type]

    assert bridges["testraum"].applied == []
    assert _counter_value(metrics, "testraum", "speed", "invalid") == 1


async def test_handler_counts_malformed_subject() -> None:
    metrics = Metrics()
    handler, _ = _handler(metrics, testraum=FakeBridge())

    await handler._on_command(FakeMsg("dyson.testraum.power", _payload(True)))  # type: ignore[arg-type]

    assert _counter_value(metrics, "unknown", "unknown", "invalid") == 1


async def test_lock_blocks_other_commands_but_not_unlocking() -> None:
    metrics = Metrics()
    handler, bridges = _handler(metrics, testraum=FakeBridge())
    bridge = bridges["testraum"]

    await handler._on_command(FakeMsg("dyson.testraum.command.lock", _payload(True)))  # type: ignore[arg-type]
    assert bridge.locked is True

    # Swallowed while locked, and counted as such rather than as an error.
    await handler._on_command(FakeMsg("dyson.testraum.command.speed", _payload(7)))  # type: ignore[arg-type]
    assert bridge.applied == []
    assert _counter_value(metrics, "testraum", "speed", "locked") == 1

    # Unlocking must always get through, otherwise the device stays stuck.
    await handler._on_command(FakeMsg("dyson.testraum.command.lock", _payload(False)))  # type: ignore[arg-type]
    assert bridge.locked is False

    await handler._on_command(FakeMsg("dyson.testraum.command.speed", _payload(7)))  # type: ignore[arg-type]
    assert bridge.applied == [("speed", 7)]


async def test_handler_counts_device_errors() -> None:
    metrics = Metrics()
    handler, _ = _handler(metrics, testraum=FakeBridge(fail=True))

    await handler._on_command(FakeMsg("dyson.testraum.command.power", _payload(True)))  # type: ignore[arg-type]

    assert _counter_value(metrics, "testraum", "power", "error") == 1
