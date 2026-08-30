"""Settings from env vars (pydantic-settings); devices from YAML, secrets from files."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from nats_bridge_core import NatsSettings
from pydantic import BaseModel, ConfigDict, field_validator


class DeviceConfig(BaseModel):
    """One Dyson device: connection details plus its NATS subject namespace."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Stable slug used in NATS subjects (dyson.<name>.state), decoupled from the
    # serial so a device can be swapped without breaking consumers.
    name: str
    host: str
    serial: str
    product_type: str = "438M"
    # Stamped by Settings.load_devices() so subject construction stays local.
    subject_prefix: str = "dyson"

    @field_validator("name", "subject_prefix")
    @classmethod
    def _single_token(cls, v: str) -> str:
        if "." in v or "/" in v or " " in v or not v:
            raise ValueError("must be a non-empty single token (no dots, slashes, spaces)")
        return v

    @property
    def state_subject(self) -> str:
        return f"{self.subject_prefix}.{self.name}.state"

    @property
    def environment_subject(self) -> str:
        return f"{self.subject_prefix}.{self.name}.environment"


class Settings(NatsSettings):
    # Devices: non-secret details in a YAML file (ConfigMap), one local MQTT
    # credential per device as <credentials_dir>/<name> (Secret).
    dyson_devices_file: Path = Path("/etc/dyson-nats-bridge/devices.yaml")
    dyson_credentials_dir: Path = Path("/etc/dyson-nats-bridge/credentials")
    # Seconds between REQUEST-CURRENT-STATE / environmental polls. Devices push
    # STATE-CHANGE on their own; polling covers sensor data and missed pushes.
    poll_interval: float = 60.0
    # Keep environmental sensors reporting while a fan is off (rhtm ON).
    ensure_monitoring: bool = True

    # NATS
    nats_subject_prefix: str = "dyson"
    nats_stream_name: str = "DYSON"

    @property
    def command_subject_filter(self) -> str:
        """One wildcard subscription covers every device."""
        return f"{self.nats_subject_prefix}.*.command.>"

    @field_validator("poll_interval")
    @classmethod
    def _poll_interval_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("POLL_INTERVAL must be > 0 seconds")
        return v

    def load_devices(self) -> list[DeviceConfig]:
        """Parse the devices YAML; raises on an empty list or duplicate names."""
        if not self.dyson_devices_file.exists():
            raise RuntimeError(f"DYSON_DEVICES_FILE {self.dyson_devices_file} does not exist")
        data: Any = yaml.safe_load(self.dyson_devices_file.read_text()) or {}
        if not isinstance(data, dict) or not isinstance(data.get("devices"), list):
            raise RuntimeError(f"{self.dyson_devices_file} must contain a top-level 'devices' list")

        devices: list[DeviceConfig] = []
        for entry in data["devices"]:
            if not isinstance(entry, dict):
                raise RuntimeError(f"{self.dyson_devices_file}: each device must be a mapping")
            devices.append(DeviceConfig(**{**entry, "subject_prefix": self.nats_subject_prefix}))
        if not devices:
            raise RuntimeError(f"{self.dyson_devices_file} declares no devices")

        names = [d.name for d in devices]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise RuntimeError(f"duplicate device names in {self.dyson_devices_file}: {duplicates}")
        return devices

    def read_device_credential(self, device_name: str) -> str:
        path = self.dyson_credentials_dir / device_name
        if not path.exists():
            raise RuntimeError(f"credential file {path} for device {device_name!r} does not exist")
        credential = path.read_text().strip()
        if not credential:
            raise RuntimeError(f"credential file {path} for device {device_name!r} is empty")
        return credential
