"""Command subscription: dyson.<device>.command.<function> {"value": ...} -> device."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from nats.aio.msg import Msg

from .config import Settings
from .device import COMMAND_FUNCTIONS, DysonBridge
from .metrics import Metrics
from .publisher import Publisher

logger = logging.getLogger(__name__)

_SPEED_MIN = 0  # 0 = auto mode (GA semantics), 1..10 = manual
_SPEED_MAX = 10
_OSCILLATION_MODE_MAX = 4  # 0 = off, 1..4 = 45/90/180/350 degrees


def split_subject(subject: str) -> tuple[str, str]:
    """Split dyson.<device>.command.<function> into (device, function).

    Raises ValueError on anything that doesn't match, so a stray subject can
    never be routed to the wrong device.
    """
    parts = subject.split(".")
    if len(parts) != 4 or parts[2] != "command":
        raise ValueError(f"malformed command subject {subject!r}")
    return parts[1], parts[3]


def parse_command(subject: str, data: bytes) -> tuple[str, str, Any]:
    """Validate subject + payload; returns (device, function, value) or raises ValueError."""
    device, function = split_subject(subject)
    if function not in COMMAND_FUNCTIONS:
        raise ValueError(f"unknown command function {function!r}")

    try:
        payload = json.loads(data)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON payload: {exc}") from exc
    if not isinstance(payload, dict) or "value" not in payload:
        raise ValueError('payload must be an object with a "value" field')
    value = payload["value"]

    if function == "speed":
        # Bools rejected: True would silently become speed 1.
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError(f"speed must be a number, got {value!r}")
        value = int(value)
        if not _SPEED_MIN <= value <= _SPEED_MAX:
            raise ValueError(f"speed must be {_SPEED_MIN}..{_SPEED_MAX}, got {value}")
        return device, function, value

    if function == "oscillation":
        # Mode enum from the GA (0 = off, 1..4 = angle preset); booleans keep
        # working for manual testing (true = on with the last angle).
        if isinstance(value, bool):
            return device, function, value
        if isinstance(value, int | float) and 0 <= int(value) <= _OSCILLATION_MODE_MAX:
            return device, function, int(value)
        raise ValueError(
            f"oscillation must be 0..{_OSCILLATION_MODE_MAX} or a boolean, got {value!r}"
        )

    # Remaining functions are switches; accept bool or 0/1 (DPT 1.001 decodes to bool,
    # but tolerate numeric writes from manual `nats pub` testing).
    if isinstance(value, bool):
        return device, function, value
    if isinstance(value, int | float) and value in (0, 1):
        return device, function, bool(value)
    raise ValueError(f"{function} must be a boolean, got {value!r}")


class CommandHandler:
    """One wildcard subscription for every device; routes by the subject's device token."""

    def __init__(
        self,
        settings: Settings,
        bridges: dict[str, DysonBridge],
        publisher: Publisher,
        metrics: Metrics,
    ) -> None:
        self._settings = settings
        self._bridges = bridges
        self._publisher = publisher
        self._metrics = metrics

    async def start(self) -> None:
        await self._publisher.subscribe_core(
            self._settings.command_subject_filter, self._on_command
        )

    def _count(self, device: str, function: str, outcome: str) -> None:
        self._metrics.commands.labels(device=device, function=function, outcome=outcome).inc()

    async def _on_command(self, msg: Msg) -> None:
        # Best-effort labels for the failure paths; parse_command() refines them.
        try:
            device, function = split_subject(msg.subject)
        except ValueError:
            device, function = "unknown", "unknown"

        try:
            device, function, value = parse_command(msg.subject, msg.data)
        except ValueError as exc:
            self._count(device, function, "invalid")
            logger.warning("invalid command on %s: %s", msg.subject, exc)
            return

        bridge = self._bridges.get(device)
        if bridge is None:
            self._count(device, function, "unknown_device")
            logger.warning("command for unknown device %r on %s", device, msg.subject)
            return

        try:
            await asyncio.to_thread(bridge.apply_command, function, value)
        except Exception as exc:
            self._count(device, function, "error")
            logger.warning("[%s] command %s=%r failed: %s", device, function, value, exc)
            return

        self._count(device, function, "ok")
        logger.info("[%s] command applied: %s=%r", device, function, value)
