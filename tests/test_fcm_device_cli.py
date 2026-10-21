import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import Mock

from local_tools.fcm_device_test import (
    build_single_device_message,
    build_test_payload,
    create_parser,
    run_cli,
)


class FcmDeviceCliTests(unittest.TestCase):
    def test_parser_rejects_topic_condition_and_multicast_destinations(self):
        forbidden_arguments = (
            ("--topic", "all_users"),
            ("--condition", "'all_users' in topics"),
            ("--tokens", "token-1,token-2"),
            ("--token-list", "tokens.txt"),
        )
        for forbidden in forbidden_arguments:
            with self.subTest(argument=forbidden[0]):
                with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                    create_parser().parse_args(
                        [
                            "--type",
                            "announcement",
                            "--device-label",
                            "USB phone",
                            *forbidden,
                        ]
                    )

    def test_message_can_target_only_one_token(self):
        payload = build_test_payload(
            "knowledge_file",
            event_id="local-test:knowledge_file:unique-1",
        )
        message = build_single_device_message(
            token="single_device_token_1234567890",
            payload=payload,
        )

        self.assertEqual(message.token, "single_device_token_1234567890")
        self.assertFalse(hasattr(message, "topic"))
        self.assertFalse(hasattr(message, "condition"))
        self.assertFalse(hasattr(message, "tokens"))
        self.assertEqual(payload.data["test_only"], "true")

    def test_dry_run_is_default_and_never_initializes_or_sends(self):
        initializer = Mock()
        sender = Mock()
        output = StringIO()
        with redirect_stdout(output):
            result = run_cli(
                ["--type", "app_update", "--device-label", "USB phone"],
                input_fn=lambda _: "USB phone | app_update",
                token_input_fn=lambda _: "single_device_token_1234567890",
                firebase_initializer=initializer,
                sender=sender,
            )

        self.assertEqual(result, 0)
        initializer.assert_not_called()
        sender.assert_not_called()
        self.assertIn('"mode": "DRY-RUN"', output.getvalue())
        self.assertNotIn("single_device_token_1234567890", output.getvalue())

    def test_send_flag_still_requires_second_confirmation(self):
        initializer = Mock()
        sender = Mock()
        answers = iter(("USB phone | announcement", "do not send"))
        with redirect_stdout(StringIO()):
            result = run_cli(
                [
                    "--type",
                    "announcement",
                    "--device-label",
                    "USB phone",
                    "--send",
                ],
                input_fn=lambda _: next(answers),
                token_input_fn=lambda _: "single_device_token_1234567890",
                firebase_initializer=initializer,
                sender=sender,
            )

        self.assertEqual(result, 2)
        initializer.assert_not_called()
        sender.assert_not_called()

    def test_each_payload_has_a_unique_test_event_id(self):
        for notification_type in ("announcement", "knowledge_file", "app_update"):
            with self.subTest(notification_type=notification_type):
                first = build_test_payload(notification_type)
                second = build_test_payload(notification_type)
                self.assertNotEqual(first.event_id, second.event_id)
                self.assertEqual(first.data["event_id"], first.event_id)
                self.assertIn("TEST", first.title)
                self.assertIn("TEST", first.body)

    def test_exact_send_confirmation_still_cannot_initialize_firebase(self):
        initializer = Mock()
        sender = Mock()
        answers = iter(("USB phone | announcement", "SEND ONE TEST MESSAGE"))
        with redirect_stdout(StringIO()):
            result = run_cli(
                [
                    "--type",
                    "announcement",
                    "--device-label",
                    "USB phone",
                    "--send",
                ],
                input_fn=lambda _: next(answers),
                token_input_fn=lambda _: "single_device_token_1234567890",
                firebase_initializer=initializer,
                sender=sender,
            )
        self.assertEqual(result, 2)
        initializer.assert_not_called()
        sender.assert_not_called()


if __name__ == "__main__":
    unittest.main()
