"""Fail-closed configuration and adapters for local notification testing.

This module is intentionally dependency-light so it can validate the process
before Firebase, Firestore, OpenAI, or FCM clients are constructed.
"""

from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


LOCAL_TEST_ENV = "KUNIV_LOCAL_NOTIFICATION_TEST"
EMULATOR_PROJECT_ID = "demo-kuniv-ai-notifications-test"
PRODUCTION_FIREBASE_PROJECT_ID = "kuwaitchat-ai-project"


def enabled() -> bool:
    return os.getenv(LOCAL_TEST_ENV) == "1"


def _loopback_host_port(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value or ":" not in value or "://" in value:
        raise RuntimeError(f"{name} must be a host:port value on loopback.")
    host, port = value.rsplit(":", 1)
    if host not in {"127.0.0.1", "localhost", "::1", "[::1]"}:
        raise RuntimeError(f"{name} must use a loopback host.")
    if not port.isdigit() or not (1 <= int(port) <= 65535):
        raise RuntimeError(f"{name} has an invalid port.")
    return value


@dataclass(frozen=True)
class LocalTestSettings:
    project_id: str
    firestore_emulator_host: str
    auth_emulator_host: str
    delivery_mode: str
    capture_path: Path
    bridge_url: str | None
    bridge_secret: str | None


def validate_environment() -> LocalTestSettings:
    """Validate every isolation invariant or abort startup."""
    if not enabled():
        raise RuntimeError("Local notification test mode is not enabled.")

    project_id = (
        os.getenv("GCLOUD_PROJECT")
        or os.getenv("GOOGLE_CLOUD_PROJECT")
        or ""
    ).strip()
    expected_project_id = os.getenv(
        "KUNIV_TEST_FIREBASE_PROJECT_ID", EMULATOR_PROJECT_ID
    ).strip()
    if (
        not expected_project_id
        or expected_project_id == PRODUCTION_FIREBASE_PROJECT_ID
        or project_id != expected_project_id
    ):
        raise RuntimeError(
            "Local test mode requires the explicitly selected non-production "
            "Firebase project."
        )

    firestore_host = _loopback_host_port("FIRESTORE_EMULATOR_HOST")
    auth_host = _loopback_host_port("FIREBASE_AUTH_EMULATOR_HOST")

    forbidden = {
        name: os.getenv(name)
        for name in (
            "FIREBASE_SERVICE_ACCOUNT_JSON",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "OPENAI_API_KEY",
        )
        if os.getenv(name)
    }
    if forbidden:
        raise RuntimeError(
            "Local test mode refuses production-capable credentials: "
            + ", ".join(sorted(forbidden))
        )
    if os.getenv("OPENAI_MOCK_MODE") != "1":
        raise RuntimeError("Local test mode requires OPENAI_MOCK_MODE=1.")

    delivery_mode = os.getenv("LOCAL_FCM_DELIVERY", "mock").strip().lower()
    if delivery_mode not in {"mock", "bridge"}:
        raise RuntimeError("LOCAL_FCM_DELIVERY must be 'mock' or 'bridge'.")

    capture_path = Path(
        os.getenv("LOCAL_FCM_CAPTURE_PATH", ".local-test/fcm-capture.jsonl")
    ).resolve()
    repository_root = Path(__file__).resolve().parents[1]
    try:
        capture_path.relative_to(repository_root)
    except ValueError as exc:
        raise RuntimeError("LOCAL_FCM_CAPTURE_PATH must stay inside the repository.") from exc

    bridge_url = None
    bridge_secret = None
    if delivery_mode == "bridge":
        if os.getenv("ALLOW_REAL_SINGLE_DEVICE_FCM") != "I_APPROVE_ONE_DEVICE":
            raise RuntimeError(
                "Bridge delivery requires ALLOW_REAL_SINGLE_DEVICE_FCM="
                "I_APPROVE_ONE_DEVICE."
            )
        bridge_url = os.getenv("LOCAL_FCM_BRIDGE_URL", "").strip()
        parsed = urlparse(bridge_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or not parsed.port
            or parsed.path not in {"", "/send"}
        ):
            raise RuntimeError("LOCAL_FCM_BRIDGE_URL must be a loopback /send URL.")
        bridge_secret = os.getenv("LOCAL_FCM_BRIDGE_SECRET", "")
        if len(bridge_secret) < 32:
            raise RuntimeError("LOCAL_FCM_BRIDGE_SECRET must be at least 32 characters.")

    return LocalTestSettings(
        project_id=project_id,
        firestore_emulator_host=firestore_host,
        auth_emulator_host=auth_host,
        delivery_mode=delivery_mode,
        capture_path=capture_path,
        bridge_url=bridge_url,
        bridge_secret=bridge_secret,
    )


def deliver_local_test_message(
    *, title: str, body: str, data: dict[str, str]
) -> tuple[str, str]:
    """Capture a message or forward it to the isolated one-token bridge.

    No topic, condition, token list, or caller-selected destination is accepted
    by this API. The bridge owns the sole in-memory registration token.
    """
    settings = validate_environment()
    envelope = {
        "destination": "single_device_only",
        "notification": {"title": title, "body": body},
        "data": {key: str(value) for key, value in data.items()},
    }
    if settings.delivery_mode == "mock":
        settings.capture_path.parent.mkdir(parents=True, exist_ok=True)
        with settings.capture_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(envelope, ensure_ascii=False) + "\n")
        event_id = envelope["data"].get("event_id", "unknown")
        return f"mock:{event_id}", "local_mock_single_device"

    request = urllib.request.Request(
        settings.bridge_url,
        data=json.dumps(envelope).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-KUNIV-Bridge-Secret": settings.bridge_secret,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
        result = json.loads(response.read().decode("utf-8"))
    message_id = str(result.get("messageId") or "")
    if not message_id:
        raise RuntimeError("The local FCM bridge returned no message ID.")
    return message_id, "local_single_device_bridge"
