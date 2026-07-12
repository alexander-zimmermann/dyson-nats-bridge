"""One-time credential bootstrap: MyDyson login (email OTP) -> local MQTT credentials.

Run locally, never in the cluster. After this, the bridge is cloud-free:
    uv run dyson-cloud-fetch --email you@example.com --region DE [--host <device-ip>]

With --host, the fetched credentials are smoke-tested against the device's
local MQTT broker (connect, request state + environmental data, print the
normalized payloads) — proving fully-local operation before any deployment.
"""

from __future__ import annotations

import argparse
import getpass
import sys
import time
from typing import Any

from libdyson import get_device
from libdyson.cloud import DysonAccount

from ..normalize import normalize_environment, normalize_state

_SMOKE_WAIT_SECONDS = 5.0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", help="MyDyson account email")
    parser.add_argument("--region", default="DE", help="Two-letter account region (default: DE)")
    parser.add_argument("--host", help="Device IP/hostname for an optional local smoke test")
    parser.add_argument(
        "--credential-out",
        help="Write the selected device's local credential to this file (mode 0600)",
    )
    # Offline smoke test with already-fetched values (skips the cloud + OTP,
    # which MyDyson rate-limits): --serial + --product-type + --host, credential
    # prompted interactively so it stays out of the shell history.
    parser.add_argument("--serial", help="Device serial (offline smoke test)")
    parser.add_argument("--product-type", help="Combined MQTT type, e.g. 438M (offline)")
    args = parser.parse_args()

    if args.serial or args.product_type:
        if not (args.serial and args.product_type and args.host):
            parser.error("offline mode needs --serial, --product-type, and --host")
        credential = getpass.getpass("device local credential: ")
        _smoke_test(args.serial, credential, args.product_type, args.host)
        return

    if not args.email:
        parser.error("--email is required (or use offline mode via --serial/--product-type)")

    account = DysonAccount()
    verify = account.login_email_otp(args.email, args.region)
    password = getpass.getpass("MyDyson account password: ")
    otp = input("OTP code from email: ").strip()
    verify(otp, password)

    devices = account.devices()
    if not devices:
        print("no devices registered on this account", file=sys.stderr)
        sys.exit(1)

    for i, info in enumerate(devices):
        print(f"[{i}] {info.name}: serial={info.serial} product_type={_mqtt_type(info)}")

    index = 0
    if len(devices) > 1:
        index = int(input(f"select device [0-{len(devices) - 1}]: ").strip())
    info = devices[index]

    print()
    print(f"serial:       {info.serial}")
    print(f"product_type: {_mqtt_type(info)} (api: type={info.product_type!r} "
          f"variant={info.variant!r})")
    print(f"credential:   {info.credential}")

    if args.credential_out:
        from pathlib import Path

        out = Path(args.credential_out)
        out.write_text(info.credential)
        out.chmod(0o600)
        print(f"credential written to {out}")

    if args.host:
        _smoke_test(info.serial, info.credential, _mqtt_type(info), args.host)


def _mqtt_type(info: Any) -> str:
    """Combine the API's product_type and variant into the MQTT topic prefix.

    The MyDyson API reports e.g. product_type="438" variant="M" separately,
    but the device's MQTT topics use the combined "438M". Some responses
    already ship the combined form in product_type, so only append when missing.
    """
    product_type: str = info.product_type
    variant: str = info.variant or ""
    if variant and not product_type.endswith(variant):
        return product_type + variant
    return product_type


def _smoke_test(serial: str, credential: str, product_type: str, host: str) -> None:
    print()
    print(f"connecting to {host} (local MQTT) ...")
    device = get_device(serial, credential, product_type)
    if device is None:
        print(f"product type {product_type!r} not supported by libdyson", file=sys.stderr)
        sys.exit(1)
    device.connect(host)
    print("connected; requesting state + environmental data ...")
    device.request_current_status()
    device.request_environmental_data()
    time.sleep(_SMOKE_WAIT_SECONDS)
    print(f"state:       {normalize_state(device)}")
    print(f"environment: {normalize_environment(device)}")
    device.disconnect()
    print("smoke test OK — local control works without the cloud")


if __name__ == "__main__":
    main()
