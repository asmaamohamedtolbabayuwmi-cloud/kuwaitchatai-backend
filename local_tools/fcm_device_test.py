"""Local, single-device FCM smoke-test CLI.

Run from the repository root with:
    python -m local_tools.fcm_device_test --type announcement --device-label "My USB phone"

Dry-run is the default. The registration token is read with hidden input and is
never stored by this utility.
"""

from __future__ import annotations

import argparse
import getpass
import json
import re
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_:\-]{20,4096}$")
_TEST_TYPES = ("announcement", "knowledge_file", "app_update")


@dataclass(frozen=True)
class TestPayload:
    notification_type: str
    event_id: str
    title: str
    body: str
    data: dict[str, str]


@dataclass(frozen=True)
class SingleDeviceMessage:
    token: str
    payload: TestPayload


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Dry-run or send one test FCM message to one Android token."
    )
    parser.add_argument("--type", required=True, choices=_TEST_TYPES)
    parser.add_argument(
        "--device-label",
        required=True,
        help="A human-readable label used only for interactive confirmation.",
    )
    parser.add_argument(
        "--send",
        action="store_true",
        help="Reserved safety flag; real sends require fcm_single_device_bridge.",
    )
    return parser


def build_test_payload(
    notification_type: str,
    *,
    event_id: str | None = None,
) -> TestPayload:
    if notification_type not in _TEST_TYPES:
        raise ValueError("Unsupported test notification type.")

    unique_id = event_id or f"local-test:{notification_type}:{uuid.uuid4().hex}"
    if notification_type == "announcement":
        title = "[KUNIV AI TEST] Announcement"
        body = "[TEST ONLY] Single-device announcement notification test."
        data = {
            "type": notification_type,
            "event_id": unique_id,
            "announcement_id": unique_id,
        }
    elif notification_type == "knowledge_file":
        title = "[KUNIV AI TEST] Knowledge file"
        body = "[TEST ONLY] No file was uploaded or indexed."
        data = {
            "type": notification_type,
            "event_id": unique_id,
            "file_id": unique_id,
            "filename": "TEST_ONLY_no_file_uploaded.pdf",
        }
    else:
        title = "[KUNIV AI TEST] App update"
        body = "[TEST ONLY] No application update was published."
        data = {
            "type": notification_type,
            "event_id": unique_id,
            "version": "999.0.0",
            "target_version": "0.0.0",
            "force": "false",
        }

    data.update({"title": title, "body": body, "test_only": "true"})
    return TestPayload(notification_type, unique_id, title, body, data)


def build_single_device_message(
    *, token: str, payload: TestPayload
) -> SingleDeviceMessage:
    cleaned_token = token.strip()
    if not _TOKEN_RE.fullmatch(cleaned_token):
        raise ValueError("The FCM registration token is missing or malformed.")
    return SingleDeviceMessage(token=cleaned_token, payload=payload)


def redacted_preview(
    *, device_label: str, token: str, payload: TestPayload, will_send: bool
) -> dict[str, Any]:
    cleaned_token = token.strip()
    redacted_token = (
        f"{cleaned_token[:6]}...{cleaned_token[-4:]}"
        if len(cleaned_token) >= 12
        else "[REDACTED]"
    )
    return {
        "mode": "SEND" if will_send else "DRY-RUN",
        "destination": {
            "kind": "single_device_token",
            "device_label": device_label,
            "token": redacted_token,
        },
        "notification": {"title": payload.title, "body": payload.body},
        "data": payload.data,
    }


def run_cli(
    argv: Sequence[str] | None = None,
    *,
    input_fn: Callable[[str], str] = input,
    token_input_fn: Callable[[str], str] = getpass.getpass,
    firebase_initializer: Callable[[], None] | None = None,
    sender: Callable[[Any], str] | None = None,
) -> int:
    args = create_parser().parse_args(argv)
    device_label = " ".join(args.device_label.split()).strip()
    if not device_label:
        raise ValueError("--device-label must not be blank.")

    token = token_input_fn("FCM registration token (hidden input): ").strip()
    payload = build_test_payload(args.type)
    message = build_single_device_message(token=token, payload=payload)

    expected = f"{device_label} | {args.type}"
    confirmation = input_fn(
        f'To confirm the device and payload type, enter exactly "{expected}": '
    ).strip()
    if confirmation != expected:
        print("Confirmation did not match; nothing was sent.")
        return 2

    print(
        json.dumps(
            redacted_preview(
                device_label=device_label,
                token=token,
                payload=payload,
                will_send=args.send,
            ),
            indent=2,
            ensure_ascii=False,
        )
    )

    if not args.send:
        print("Dry-run complete. No Firebase initialization or FCM send occurred.")
        return 0

    send_confirmation = input_fn(
        'Type "SEND ONE TEST MESSAGE" to perform the single-device send: '
    ).strip()
    if send_confirmation != "SEND ONE TEST MESSAGE":
        print("Send confirmation did not match; nothing was sent.")
        return 2

    # This legacy utility deliberately cannot initialize Firebase anymore.
    # Real delivery is isolated in fcm_single_device_bridge, which validates a
    # non-production project and owns one hidden token in memory.
    print(
        "Direct sending is disabled. Start local_tools.fcm_single_device_bridge "
        "after explicit approval. Nothing was sent."
    )
    return 2



def main() -> int:
    return run_cli()


if __name__ == "__main__":
    raise SystemExit(main())
