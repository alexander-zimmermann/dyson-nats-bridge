"""Device connection owner: libdyson MQTT client, reconnect loop, polling, command apply.

libdyson is paho-callback based (its own network thread); every callback is
hopped onto the asyncio loop via call_soon_threadsafe, and every blocking
libdyson call leaves the loop via asyncio.to_thread.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from libdyson import MessageType, get_device
from libdyson.dyson_device import DysonFanDevice

from .config import DeviceConfig, Settings
from .metrics import Metrics
from .normalize import (
    OSCILLATION_PRESETS,
    normalize_environment,
    normalize_state,
    oscillation_mode,
)
from .publisher import Publisher

logger = logging.getLogger(__name__)

_RECONNECT_BACKOFF_START_SECONDS = 5.0
_RECONNECT_BACKOFF_MAX_SECONDS = 300.0

# Bounded hop queue from the paho thread to the asyncio loop; the device
# emits at most a handful of messages per second.
_MESSAGE_QUEUE_MAX = 100

COMMAND_FUNCTIONS = ("power", "speed", "oscillation", "night")


class DysonBridge:
    """Owns one device's connection and publishes its normalized state to NATS."""

    def __init__(
        self,
        settings: Settings,
        config: DeviceConfig,
        publisher: Publisher,
        metrics: Metrics,
    ) -> None:
        self._settings = settings
        self._config = config
        self._name = config.name
        self._publisher = publisher
        self._metrics = metrics
        self._device: DysonFanDevice = self._build_device()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[MessageType] = asyncio.Queue(maxsize=_MESSAGE_QUEUE_MAX)
        self._supervisor_task: asyncio.Task[None] | None = None
        self._pump_task: asyncio.Task[None] | None = None
        self._stopping = False

    def _build_device(self) -> DysonFanDevice:
        device = get_device(
            self._config.serial,
            self._settings.read_device_credential(self._config.name),
            self._config.product_type,
        )
        if device is None:
            raise RuntimeError(
                f"device {self._config.name!r}: unknown product_type {self._config.product_type!r}"
            )
        if not isinstance(device, DysonFanDevice):
            raise RuntimeError(
                f"device {self._config.name!r}: product_type "
                f"{self._config.product_type!r} is not a fan device"
            )
        return device

    @property
    def name(self) -> str:
        return self._config.name

    @property
    def is_connected(self) -> bool:
        return bool(self._device.is_connected)

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._device.add_message_listener(self._on_message)
        self._pump_task = asyncio.create_task(self._pump())
        self._supervisor_task = asyncio.create_task(self._supervise())

    async def stop(self) -> None:
        self._stopping = True
        for task in (self._supervisor_task, self._pump_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._supervisor_task = None
        self._pump_task = None
        if self._device.is_connected:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self._device.disconnect)
        self._metrics.dyson_connected.labels(device=self._name).set(0)

    # --- device -> NATS -------------------------------------------------

    def _on_message(self, message_type: MessageType) -> None:
        """libdyson callback; runs on the paho thread."""
        if self._loop is None or self._loop.is_closed():
            return
        self._loop.call_soon_threadsafe(self._enqueue_message, message_type)

    def _enqueue_message(self, message_type: MessageType) -> None:
        try:
            self._queue.put_nowait(message_type)
        except asyncio.QueueFull:
            logger.warning("[%s] device message queue full, dropping %s", self._name, message_type)

    async def _pump(self) -> None:
        while True:
            message_type = await self._queue.get()
            try:
                self._metrics.last_message_ts.labels(device=self._name).set(time.time())
                if message_type is MessageType.STATE:
                    kind, subject = "state", self._config.state_subject
                    payload = normalize_state(self._device)
                    mode = oscillation_mode(self._raw_status("oson"), self._raw_status("ancp"))
                    if mode is not None:
                        payload["oscillation_mode"] = mode
                elif message_type is MessageType.ENVIRONMENTAL:
                    kind, subject = "environment", self._config.environment_subject
                    payload = normalize_environment(self._device)
                else:
                    continue

                self._metrics.messages_received.labels(device=self._name, kind=kind).inc()
                if payload:
                    self._publisher.enqueue(self._name, kind, subject, payload)
            except Exception:
                logger.exception("[%s] error handling device message %s", self._name, message_type)

    def _raw_status(self, field: str) -> str | None:
        """Read one raw status field; handles STATE-CHANGE [old, new] pairs."""
        try:
            value = self._device._get_field_value(self._device._status, field)
        except Exception:
            return None
        return value if isinstance(value, str) else None

    # --- connection & polling -------------------------------------------

    async def _supervise(self) -> None:
        """Single loop owning connect, reconnect-with-backoff, and periodic polls."""
        backoff = _RECONNECT_BACKOFF_START_SECONDS
        while not self._stopping:
            if not self._device.is_connected:
                self._metrics.dyson_connected.labels(device=self._name).set(0)
                try:
                    await asyncio.to_thread(self._device.connect, self._config.host)
                except Exception as exc:
                    self._metrics.reconnects.labels(device=self._name, outcome="error").inc()
                    logger.warning(
                        "[%s] connect to %s failed, retrying in %.0fs: %s",
                        self._name,
                        self._config.host,
                        backoff,
                        exc,
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, _RECONNECT_BACKOFF_MAX_SECONDS)
                    continue
                self._metrics.reconnects.labels(device=self._name, outcome="ok").inc()
                self._metrics.dyson_connected.labels(device=self._name).set(1)
                backoff = _RECONNECT_BACKOFF_START_SECONDS
                logger.info("[%s] connected: %s", self._name, self._config.host)
                await self._after_connect()

            await asyncio.sleep(self._settings.poll_interval)
            await self._poll()

    async def _after_connect(self) -> None:
        if self._settings.ensure_monitoring:
            try:
                await asyncio.to_thread(self._device.enable_continuous_monitoring)
            except Exception:
                logger.exception("[%s] enabling continuous monitoring failed", self._name)
        await self._poll()

    async def _poll(self) -> None:
        if not self._device.is_connected:
            return
        try:
            await asyncio.to_thread(self._device.request_current_status)
            await asyncio.to_thread(self._device.request_environmental_data)
        except Exception as exc:
            self._metrics.poll_errors.labels(device=self._name).inc()
            logger.warning("[%s] poll failed: %s", self._name, exc)

    # --- NATS -> device ---------------------------------------------------

    def apply_command(self, function: str, value: Any) -> None:
        """Translate one validated command into libdyson calls (blocking; call via to_thread).

        The device answers with a STATE-CHANGE push, which closes the status
        loop back to NATS/KNX — no optimistic state is published here.
        """
        device = self._device
        if function == "power":
            if value:
                device.turn_on()
            else:
                device.turn_off()
        elif function == "speed":
            if value == 0:
                # GA semantics: Stufe 0 = Automatik (implies power on).
                device.turn_on()
                device.enable_auto_mode()
            else:
                device.disable_auto_mode()
                device.set_speed(int(value))
        elif function == "oscillation":
            # libdyson's enable_oscillation() writes the TP04 dialect
            # (ancp CUST + osal/osau); newer devices (438M) take the app's
            # angle presets as a numeric ancp width instead.
            if value is True:
                device._set_configuration(oson="ON", fpwr="ON")
            elif value is False or value == 0:
                device.disable_oscillation()
            else:
                angle = OSCILLATION_PRESETS[int(value)]
                device._set_configuration(oson="ON", fpwr="ON", ancp=f"{angle:04d}")
        elif function == "night":
            if value:
                device.enable_night_mode()
            else:
                device.disable_night_mode()
        else:
            raise ValueError(f"unknown command function {function!r}")
