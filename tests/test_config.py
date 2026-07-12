"""Unit tests for Settings validation and derived subjects."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from dyson_nats_bridge.config import Settings


def _settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "dyson_host": "fan.local",
        "dyson_serial": "XX1-EU-ABC1234A",
        "dyson_device_name": "testraum",
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def test_subjects_derive_from_device_name() -> None:
    s = _settings()
    assert s.state_subject == "dyson.testraum.state"
    assert s.environment_subject == "dyson.testraum.environment"
    assert s.command_subject_filter == "dyson.testraum.command.>"


def test_device_name_must_be_single_token() -> None:
    with pytest.raises(ValidationError):
        _settings(dyson_device_name="schlafzimmer.eltern")


def test_poll_interval_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        _settings(poll_interval=0)


def test_read_dyson_credential(tmp_path: Path) -> None:
    cred = tmp_path / "credential"
    cred.write_text("s3cret\n")
    assert _settings(dyson_credential_file=cred).read_dyson_credential() == "s3cret"

    with pytest.raises(RuntimeError, match="does not exist"):
        _settings(dyson_credential_file=tmp_path / "missing").read_dyson_credential()

    empty = tmp_path / "empty"
    empty.write_text("")
    with pytest.raises(RuntimeError, match="is empty"):
        _settings(dyson_credential_file=empty).read_dyson_credential()


def test_nats_auth_precedence(tmp_path: Path) -> None:
    seed = tmp_path / "nkey-seed"
    seed.write_text("SUAB...")
    password = tmp_path / "nats-password"
    password.write_text("pw\n")

    assert _settings().nats_auth_kwargs() == {}
    assert _settings(nats_nkey_seed_file=seed).nats_auth_kwargs() == {"nkeys_seed": str(seed)}
    assert _settings(
        nats_user="dyson", nats_user_password_file=password
    ).nats_auth_kwargs() == {"user": "dyson", "password": "pw"}
    # nkey seed wins over user/password.
    assert _settings(
        nats_nkey_seed_file=seed, nats_user="dyson", nats_user_password_file=password
    ).nats_auth_kwargs() == {"nkeys_seed": str(seed)}
