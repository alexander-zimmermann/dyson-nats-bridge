"""The shipped knx.yaml names only subjects and fields the bridge actually publishes."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pytest
from libdyson import MessageType
from nats_bridge_core import knx_descriptor

from dyson_nats_bridge.config import DeviceConfig, Settings
from dyson_nats_bridge.device import DysonBridge
from dyson_nats_bridge.metrics import Metrics
from tests.test_normalize import FakeDevice

DESCRIPTOR = knx_descriptor.load_package("dyson_nats_bridge")

DEVICE = DeviceConfig(name="testraum", host="fan.local", serial="XX1-EU-XXX0000A")


class ConnectedFan(FakeDevice):
    """A fan after its first CURRENT-STATE: every reading the reference unit reports."""

    # Raw status the bridge reads past the normalizer for `oscillation_mode`
    _status = {"oson": "ON", "ancp": "0090"}

    def __init__(self) -> None:
        super().__init__(
            is_on=True,
            auto_mode=False,
            speed=4,
            oscillation=True,
            night_mode=False,
            fan_state=True,
            hepa_filter_life=96,
            temperature=294.65,
            humidity=52,
            particulate_matter_2_5=3,
            particulate_matter_10=5,
            volatile_organic_compounds=1.2,
            nitrogen_dioxide=0.4,
        )

    @staticmethod
    def _get_field_value(status: dict[str, Any], field: str) -> Any:
        return status.get(field)


class FakePublisher:
    """Records the payload per kind label; the kind is the subject suffix (device.py)."""

    def __init__(self) -> None:
        self.published: dict[str, dict[str, Any]] = {}

    def enqueue(self, ctx: tuple[str, str], subject: str, payload: dict[str, Any]) -> None:
        _, kind = ctx
        assert subject == f"dyson.{DEVICE.name}.{kind}"
        self.published[kind] = payload


@pytest.fixture
async def published(monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[str, Any]]:
    """What leaves the process, per subject suffix, after a STATE and an ENVIRONMENTAL push."""
    monkeypatch.setattr(DysonBridge, "_build_device", lambda _self: ConnectedFan())
    publisher = FakePublisher()
    bridge = DysonBridge(Settings(), DEVICE, publisher, Metrics())  # type: ignore[arg-type]

    for message_type in (MessageType.STATE, MessageType.ENVIRONMENTAL):
        bridge._enqueue_message(message_type)
    # A shut-down queue hands out what is queued, then ends the pump instead of parking it
    bridge._queue.shutdown()
    with contextlib.suppress(asyncio.QueueShutDown):
        await bridge._pump()
    return publisher.published


@pytest.mark.parametrize("suffix", list(DESCRIPTOR.subjects))
def test_every_descriptor_field_is_published_on_its_subject(
    published: dict[str, dict[str, Any]], suffix: str
) -> None:
    assert suffix in published

    missing = [name for name in DESCRIPTOR.subjects[suffix].fields if name not in published[suffix]]
    assert missing == []
