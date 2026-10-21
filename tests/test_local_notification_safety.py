import json
import os
import unittest
import asyncio
from pathlib import Path
from unittest.mock import patch

from app.local_notification_test import (
    EMULATOR_PROJECT_ID,
    deliver_local_test_message,
    validate_environment,
)
from app.services.notification_service import (
    ConfirmedPreSendNotificationError,
    FcmSender,
)


def safe_environment(capture: str) -> dict[str, str]:
    return {
        "KUNIV_LOCAL_NOTIFICATION_TEST": "1",
        "OPENAI_MOCK_MODE": "1",
        "GCLOUD_PROJECT": EMULATOR_PROJECT_ID,
        "GOOGLE_CLOUD_PROJECT": EMULATOR_PROJECT_ID,
        "KUNIV_TEST_FIREBASE_PROJECT_ID": EMULATOR_PROJECT_ID,
        "FIRESTORE_EMULATOR_HOST": "127.0.0.1:48087",
        "FIREBASE_AUTH_EMULATOR_HOST": "127.0.0.1:49099",
        "LOCAL_FCM_DELIVERY": "mock",
        "LOCAL_FCM_CAPTURE_PATH": capture,
    }


class LocalNotificationSafetyTests(unittest.TestCase):
    def test_refuses_credentials_and_non_loopback_emulator(self):
        environment = safe_environment(".local-test/safety.jsonl")
        environment["OPENAI_API_KEY"] = "must-not-be-used"
        with patch.dict(os.environ, environment, clear=True), self.assertRaises(RuntimeError):
            validate_environment()

        environment.pop("OPENAI_API_KEY")
        environment["FIRESTORE_EMULATOR_HOST"] = "firestore.googleapis.com:443"
        with patch.dict(os.environ, environment, clear=True), self.assertRaises(RuntimeError):
            validate_environment()

    def test_mock_delivery_has_no_topic_condition_or_token_list(self):
        path = Path(".local-test/safety-capture.jsonl")
        path.unlink(missing_ok=True)
        with patch.dict(os.environ, safe_environment(str(path)), clear=True):
            message_id, destination = deliver_local_test_message(
                title="Test",
                body="Body",
                data={"type": "announcement", "event_id": "event-1"},
            )
        envelope = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(message_id.startswith("mock:"))
        self.assertEqual(destination, "local_mock_single_device")
        self.assertEqual(envelope["destination"], "single_device_only")
        serialized = json.dumps(envelope).lower()
        self.assertNotIn('"topic"', serialized)
        self.assertNotIn('"condition"', serialized)
        self.assertNotIn('"tokens"', serialized)
        path.unlink(missing_ok=True)

    def test_bridge_mode_requires_explicit_approval(self):
        environment = safe_environment(".local-test/safety.jsonl")
        environment.update(
            {
                "LOCAL_FCM_DELIVERY": "bridge",
                "LOCAL_FCM_BRIDGE_URL": "http://127.0.0.1:48787/send",
                "LOCAL_FCM_BRIDGE_SECRET": "s" * 32,
            }
        )
        with patch.dict(os.environ, environment, clear=True), self.assertRaises(RuntimeError):
            validate_environment()

    def test_production_topic_sender_is_disabled_in_local_mode(self):
        with patch.dict(
            os.environ,
            safe_environment(".local-test/safety.jsonl"),
            clear=True,
        ):
            with self.assertRaises(ConfirmedPreSendNotificationError):
                asyncio.run(
                    FcmSender().send_topic(
                        topic="all_users",
                        title="Forbidden",
                        body="Forbidden",
                        data={"type": "announcement"},
                    )
                )


if __name__ == "__main__":
    unittest.main()
