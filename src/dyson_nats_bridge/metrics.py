"""Prometheus counters for this bridge. The /metrics server itself lives in core."""

from __future__ import annotations

import logging
from typing import cast

from nats_bridge_core import TrackedStreamHandler
from prometheus_client import CollectorRegistry, Counter, Gauge

logger = logging.getLogger(__name__)


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()

        self.dyson_connected = Gauge(
            "dyson_connected",
            "1 if the device MQTT connection is currently up, 0 otherwise",
            ["device"],
            registry=self.registry,
        )
        self.nats_connected = Gauge(
            "nats_connected",
            "1 if NATS client is currently connected, 0 otherwise",
            registry=self.registry,
        )
        # Exposed for alerting: the filter has to be replaced before it runs
        # out, and nothing else in the stack watches a slow-moving value.
        self.filter_life = Gauge(
            "dyson_filter_life_percent",
            "Remaining HEPA filter life in percent",
            ["device"],
            registry=self.registry,
        )
        self.messages_received = Counter(
            "dyson_messages_received_total",
            "Messages received from the device by kind (state | environment)",
            ["device", "kind"],
            registry=self.registry,
        )
        self.messages_published = Counter(
            "dyson_messages_published_total",
            "Normalized messages successfully published to NATS by kind",
            ["device", "kind"],
            registry=self.registry,
        )
        self.publish_errors = Counter(
            "dyson_publish_errors_total",
            "Publish errors by reason",
            ["device", "reason"],
            registry=self.registry,
        )
        self.commands = Counter(
            "dyson_commands_total",
            "Commands received on NATS by function and outcome (ok | invalid | error)",
            ["device", "function", "outcome"],
            registry=self.registry,
        )
        self.poll_errors = Counter(
            "dyson_poll_errors_total",
            "Failed state/environment poll requests to the device",
            ["device"],
            registry=self.registry,
        )
        self.reconnects = Counter(
            "dyson_reconnects_total",
            "Device MQTT reconnect attempts by outcome (ok | error)",
            ["device", "outcome"],
            registry=self.registry,
        )
        self.last_message_ts = Gauge(
            "dyson_last_message_received_timestamp",
            "Unix timestamp of the last message received from the device (seconds)",
            ["device"],
            registry=self.registry,
        )
        # Surface logger-health state so a stuck stdout is visible in Prometheus,
        # not just via liveness. Source of truth is TrackedStreamHandler.
        self.log_emit_errors = Gauge(
            "dyson_bridge_log_emit_errors",
            "Cumulative count of logging handler emit() failures since pod start",
            registry=self.registry,
        )
        self.log_emit_errors.set_function(lambda: float(TrackedStreamHandler.emit_errors_total))
        self.log_last_emit_ok_timestamp = Gauge(
            "dyson_bridge_log_last_emit_ok_timestamp",
            "Monotonic-seconds timestamp of the last successful log emit",
            registry=self.registry,
        )
        self.log_last_emit_ok_timestamp.set_function(
            lambda: float(TrackedStreamHandler.last_emit_ok_ts)
        )

    # --- nats_bridge_core.PublisherMetrics -------------------------------
    # The publisher hands back the ctx given to enqueue(); here that is the
    # (device, kind) pair the counters are labelled by.

    def set_connected(self, connected: bool) -> None:
        self.nats_connected.set(1 if connected else 0)

    def count_published(self, ctx: object) -> None:
        device, kind = cast(tuple[str, str], ctx)
        self.messages_published.labels(device=device, kind=kind).inc()

    def count_error(self, ctx: object, reason: str) -> None:
        device, _ = cast(tuple[str, str], ctx)
        self.publish_errors.labels(device=device, reason=reason).inc()
