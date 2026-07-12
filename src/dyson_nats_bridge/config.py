"""Settings from env vars (pydantic-settings); secrets are read from files."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class LogFormat(StrEnum):
    JSON = "json"
    TEXT = "text"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Dyson device (the device runs its own MQTT broker; we connect to it)
    dyson_host: str
    dyson_serial: str
    dyson_credential_file: Path = Path("/etc/dyson-nats-bridge/credential")
    dyson_product_type: str = "438M"
    # Stable slug used in NATS subjects (dyson.<device_name>.state), decoupled
    # from the serial so the device can be swapped without breaking consumers.
    dyson_device_name: str
    # Seconds between REQUEST-CURRENT-STATE / environmental polls. The device
    # pushes STATE-CHANGE on its own; polling covers sensor data and missed pushes.
    poll_interval: float = 60.0
    # Keep environmental sensors reporting while the fan is off (rhtm ON).
    ensure_monitoring: bool = True

    # NATS
    nats_servers: str = "nats://localhost:4222"
    nats_subject_prefix: str = "dyson"
    nats_creds_file: Path | None = None
    nats_nkey_seed_file: Path | None = None
    nats_user: str | None = None
    nats_user_password_file: Path | None = None
    nats_stream_check: bool = True
    nats_stream_name: str = "DYSON"

    # Observability
    metrics_port: int = 9090
    log_level: str = "INFO"
    log_format: LogFormat = LogFormat.JSON

    @property
    def nats_servers_list(self) -> list[str]:
        return [s.strip() for s in self.nats_servers.split(",") if s.strip()]

    @property
    def state_subject(self) -> str:
        return f"{self.nats_subject_prefix}.{self.dyson_device_name}.state"

    @property
    def environment_subject(self) -> str:
        return f"{self.nats_subject_prefix}.{self.dyson_device_name}.environment"

    @property
    def command_subject_filter(self) -> str:
        return f"{self.nats_subject_prefix}.{self.dyson_device_name}.command.>"

    @field_validator("poll_interval")
    @classmethod
    def _poll_interval_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("POLL_INTERVAL must be > 0 seconds")
        return v

    @field_validator("nats_subject_prefix", "dyson_device_name")
    @classmethod
    def _single_token(cls, v: str) -> str:
        if "." in v or "/" in v or " " in v or not v:
            raise ValueError("must be a non-empty single token (no dots, slashes, spaces)")
        return v

    def read_dyson_credential(self) -> str:
        if not self.dyson_credential_file.exists():
            raise RuntimeError(f"DYSON_CREDENTIAL_FILE {self.dyson_credential_file} does not exist")
        credential = self.dyson_credential_file.read_text().strip()
        if not credential:
            raise RuntimeError(f"DYSON_CREDENTIAL_FILE {self.dyson_credential_file} is empty")
        return credential

    def read_nats_password(self) -> str | None:
        if self.nats_user_password_file and self.nats_user_password_file.exists():
            return self.nats_user_password_file.read_text().strip()
        return None

    def nats_auth_kwargs(self) -> dict[str, Any]:
        """Build the auth subset of NatsClient.connect kwargs.

        Auth precedence: creds file > nkey seed file > user/password.
        Each form is mutually exclusive in nats-py; pick the first that's configured.
        """
        kwargs: dict[str, Any] = {}
        if self.nats_creds_file and self.nats_creds_file.exists():
            kwargs["user_credentials"] = str(self.nats_creds_file)
        elif self.nats_nkey_seed_file and self.nats_nkey_seed_file.exists():
            kwargs["nkeys_seed"] = str(self.nats_nkey_seed_file)
        elif self.nats_user:
            password = self.read_nats_password()
            if password is None:
                raise RuntimeError(
                    "NATS_USER is set but NATS_USER_PASSWORD_FILE is missing or empty"
                )
            kwargs["user"] = self.nats_user
            kwargs["password"] = password
        return kwargs
