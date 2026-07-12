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


def parse_command(subject: str, data: bytes) -> tuple[str, Any]:
    """Validate subject + payload; returns (function, value) or raises ValueError."""
    function = subject.rsplit(".", 1)[-1]
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
        # Accept bools rejected: True would silently become speed 1.
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError(f"speed must be a number, got {value!r}")
        value = int(value)
        if not _SPEED_MIN <= value <= _SPEED_MAX:
            raise ValueError(f"speed must be {_SPEED_MIN}..{_SPEED_MAX}, got {value}")
        return function, value

    # Remaining functions are switches; accept bool or 0/1 (DPT 1.001 decodes to bool,
    # but tolerate numeric writes from manual `nats pub` testing).
    if isinstance(value, bool):
        return function, value
    if isinstance(value, int | float) and value in (0, 1):
        return function, bool(value)
    raise ValueError(f"{function} must be a boolean, got {value!r}")


class CommandHandler:
    def __init__(
        self,
        settings: Settings,
        bridge: DysonBridge,
        publisher: Publisher,
        metrics: Metrics,
    ) -> None:
        self._settings = settings
        self._bridge = bridge
        self._publisher = publisher
        self._metrics = metrics

    async def start(self) -> None:
        await self._publisher.subscribe_core(
            self._settings.command_subject_filter, self._on_command
        )

    async def _on_command(self, msg: Msg) -> None:
        function = msg.subject.rsplit(".", 1)[-1]
        try:
            function, value = parse_command(msg.subject, msg.data)
        except ValueError as exc:
            self._metrics.commands.labels(function=function, outcome="invalid").inc()
            logger.warning("invalid command on %s: %s", msg.subject, exc)
            return

        try:
            await asyncio.to_thread(self._bridge.apply_command, function, value)
        except Exception as exc:
            self._metrics.commands.labels(function=function, outcome="error").inc()
            logger.warning("command %s=%r failed: %s", function, value, exc)
            return

        self._metrics.commands.labels(function=function, outcome="ok").inc()
        logger.info("command applied: %s=%r", function, value)
