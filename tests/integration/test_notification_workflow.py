"""Firestore-emulator integration coverage for the notification workflow.

This module intentionally refuses to import application notification code until
the emulator, demo project, and credential guards have passed. FCM and OpenAI
are always replaced with local fakes and their real entry points are tripwired.
"""

from __future__ import annotations

import asyncio
import os
import unittest
import uuid
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit


DEMO_PROJECT_PREFIX = "demo-"
REQUIRED_RUN_FLAG = "RUN_NOTIFICATION_INTEGRATION_TESTS"
SENSITIVE_ENVIRONMENT = (
    "FIREBASE_SERVICE_ACCOUNT_JSON",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "OPENAI_API_KEY",
)
TEST_ANALYTICS_PEPPER = "notification-integration-test-pepper-only"


def require_isolated_environment() -> tuple[str, str]:
    if os.getenv(REQUIRED_RUN_FLAG) != "1":
        raise RuntimeError(
            "Refusing notification integration tests without the guarded runner."
        )

    emulator = os.getenv("FIRESTORE_EMULATOR_HOST", "").strip()
    parsed = urlsplit(f"//{emulator}")
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or not parsed.port:
        raise RuntimeError("FIRESTORE_EMULATOR_HOST must be an explicit loopback host and port.")
    auth_emulator = os.getenv("FIREBASE_AUTH_EMULATOR_HOST", "").strip()
    auth_parsed = urlsplit(f"//{auth_emulator}")
    if auth_parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or not auth_parsed.port:
        raise RuntimeError("FIREBASE_AUTH_EMULATOR_HOST must be loopback.")

    projects = {
        value
        for value in (
            os.getenv("GCLOUD_PROJECT"),
            os.getenv("GOOGLE_CLOUD_PROJECT"),
        )
        if value
    }
    if len(projects) != 1:
        raise RuntimeError("GCLOUD_PROJECT and GOOGLE_CLOUD_PROJECT must name one project.")
    project = projects.pop()
    if not project.startswith(DEMO_PROJECT_PREFIX):
        raise RuntimeError("The integration project must use Firebase's demo- namespace.")

    present = [name for name in SENSITIVE_ENVIRONMENT if os.getenv(name)]
    if present:
        raise RuntimeError(
            "Production-capable credentials are forbidden: " + ", ".join(present)
        )
    if os.getenv("ANALYTICS_ID_PEPPER") != TEST_ANALYTICS_PEPPER:
        raise RuntimeError("The integration analytics pepper is missing or not test-only.")
    return emulator, project


EMULATOR_HOST, PROJECT_ID = require_isolated_environment()

from fastapi import FastAPI  # noqa: E402
from google.auth.credentials import AnonymousCredentials  # noqa: E402
from google.cloud.firestore_v1.async_client import AsyncClient  # noqa: E402
from httpx import ASGITransport, AsyncClient as HttpClient  # noqa: E402

from app.auth import verify_admin  # noqa: E402
from app.routers import notifications as notification_routes  # noqa: E402
from app.services.knowledge_file_notifications import (  # noqa: E402
    process_knowledge_notification_job,
)
from app.services.notification_service import (  # noqa: E402
    ANNOUNCEMENTS_COLLECTION,
    KNOWLEDGE_JOBS_COLLECTION,
    DeliveryResult,
    GENERAL_AUDIENCE_CONDITION,
    NotificationRepository,
    NotificationService,
)


class FakeFcmSender:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def send_topic(self, **kwargs):
        self.calls.append({"method": "topic", **kwargs})
        if self.error:
            raise self.error
        return DeliveryResult("fake-fcm-message", str(kwargs["topic"]))

    async def send_general_audience(self, **kwargs):
        self.calls.append(
            {
                "method": "general_audience",
                "condition": GENERAL_AUDIENCE_CONDITION,
                **kwargs,
            }
        )
        if self.error:
            raise self.error
        return DeliveryResult("fake-fcm-message", "all_users")


class FakeIndexingClient:
    def __init__(self, statuses: list[str], notification_sender: FakeFcmSender) -> None:
        self._statuses = list(statuses)
        self._notification_sender = notification_sender
        self.calls = 0
        self.vector_stores = SimpleNamespace(
            files=SimpleNamespace(retrieve=self.retrieve)
        )

    async def retrieve(self, **_kwargs):
        index = min(self.calls, len(self._statuses) - 1)
        status = self._statuses[index]
        self.calls += 1
        if status != "completed" and self._notification_sender.calls:
            raise AssertionError("FCM was invoked before indexing completed.")
        return SimpleNamespace(status=status)


class NotificationWorkflowIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        # Anonymous credentials cannot authenticate to production. The emulator
        # endpoint is additionally fixed by the fail-closed module preflight.
        self.db = AsyncClient(
            project=PROJECT_ID,
            credentials=AnonymousCredentials(),
        )
        self.repository = NotificationRepository(self.db)
        self.sender = FakeFcmSender()
        self.service = NotificationService(self.repository, self.sender)
        self.app = FastAPI()
        self.app.include_router(notification_routes.router)
        self.app.dependency_overrides[verify_admin] = lambda: "integration-admin"
        self.tripwires = ExitStack()
        self.tripwires.enter_context(
            patch(
                "firebase_admin.messaging.send",
                side_effect=AssertionError("Real FCM is forbidden in integration tests."),
            )
        )
        self.tripwires.enter_context(
            patch(
                "app.services.knowledge_file_notifications.get_openai_client",
                side_effect=AssertionError("Real OpenAI is forbidden in integration tests."),
            )
        )

    async def asyncTearDown(self) -> None:
        self.tripwires.close()
        self.db.close()

    def _route_patches(self):
        stack = ExitStack()
        stack.enter_context(
            patch.object(notification_routes, "NotificationService", return_value=self.service)
        )
        stack.enter_context(
            patch.object(notification_routes, "NotificationRepository", return_value=self.repository)
        )
        return stack

    async def _client(self, app: FastAPI | None = None) -> HttpClient:
        return HttpClient(
            transport=ASGITransport(app=app or self.app),
            base_url="http://integration.test",
        )

    async def test_admin_announcement_history_and_unique_view_workflow(self) -> None:
        key = f"integration-{uuid.uuid4().hex}"
        installation = f"installation_{uuid.uuid4().hex}"
        with self._route_patches():
            async with await self._client() as client:
                created = await client.post(
                    "/notifications/announcements",
                    headers={"Idempotency-Key": key},
                    json={"title": "Integration test", "body": "Emulator only"},
                )
                self.assertEqual(created.status_code, 201, created.text)
                announcement_id = created.json()["id"]

                snapshot = await self.db.collection(ANNOUNCEMENTS_COLLECTION).document(
                    announcement_id
                ).get()
                self.assertTrue(snapshot.exists)
                saved = snapshot.to_dict()
                self.assertEqual(saved["title"], "Integration test")
                self.assertEqual(saved["body"], "Emulator only")
                self.assertEqual(saved["createdBy"], "integration-admin")
                self.assertEqual(saved["delivery"]["status"], "sent")

                latest = await client.get("/notifications/announcements/latest")
                self.assertEqual(latest.status_code, 200, latest.text)
                self.assertIn(
                    announcement_id,
                    [item["id"] for item in latest.json()["announcements"]],
                )

                first = await client.post(
                    f"/notifications/announcements/{announcement_id}/views",
                    json={"installationId": installation},
                )
                repeated = await client.post(
                    f"/notifications/announcements/{announcement_id}/views",
                    json={"installationId": installation},
                )
                self.assertEqual(first.status_code, 200, first.text)
                self.assertEqual(repeated.status_code, 200, repeated.text)
                self.assertTrue(first.json()["counted"])
                self.assertFalse(repeated.json()["counted"])

                refreshed = await self.db.collection(ANNOUNCEMENTS_COLLECTION).document(
                    announcement_id
                ).get()
                self.assertEqual(refreshed.to_dict()["viewCount"], 1)
                self.assertEqual(len(self.sender.calls), 1)
                self.assertEqual(
                    self.sender.calls[0]["condition"], GENERAL_AUDIENCE_CONDITION
                )

    async def test_unauthorized_admin_request_never_writes_or_sends(self) -> None:
        protected_app = FastAPI()
        protected_app.include_router(notification_routes.router)
        key = f"integration-{uuid.uuid4().hex}"
        with self._route_patches():
            async with await self._client(protected_app) as client:
                response = await client.post(
                    "/notifications/announcements",
                    headers={"Idempotency-Key": key},
                    json={"title": "Forbidden", "body": "Must not be saved"},
                )
        self.assertIn(response.status_code, {401, 403})
        self.assertEqual(self.sender.calls, [])
        query = self.db.collection(ANNOUNCEMENTS_COLLECTION).where(
            "idempotencyKey", "==", key
        )
        self.assertEqual([item async for item in query.stream()], [])

    async def test_ambiguous_announcement_send_is_not_retried(self) -> None:
        sender = FakeFcmSender(RuntimeError("ambiguous transport result"))
        service = NotificationService(self.repository, sender)
        key = f"integration-{uuid.uuid4().hex}"
        for _ in range(2):
            with self.assertRaises(Exception):
                await service.create_announcement(
                    "Ambiguous test", "No automatic retry", "integration-admin", key
                )
        self.assertEqual(len(sender.calls), 1)

    async def test_knowledge_notification_waits_for_completed_indexing(self) -> None:
        file_id = f"file-{uuid.uuid4().hex}"
        await self.db.collection(KNOWLEDGE_JOBS_COLLECTION).document(file_id).set(
            {"fileId": file_id, "filename": "integration.pdf", "state": "indexing", "attempts": 0}
        )
        indexing = FakeIndexingClient(["in_progress", "completed"], self.sender)
        result = await process_knowledge_notification_job(
            file_id,
            db=self.db,
            client=indexing,
            vector_store_id="vs-emulator-only",
            notification_service=self.service,
            poll_seconds=0,
            delay=lambda _seconds: asyncio.sleep(0),
        )
        self.assertEqual(result, "sent")
        self.assertEqual(indexing.calls, 2)
        self.assertEqual(len(self.sender.calls), 1)
        self.assertEqual(
            self.sender.calls[0]["condition"], GENERAL_AUDIENCE_CONDITION
        )
        self.assertEqual(self.sender.calls[0]["data"]["type"], "knowledge_file")

    async def test_uncertain_knowledge_send_is_quarantined_without_retry(self) -> None:
        file_id = f"file-{uuid.uuid4().hex}"
        await self.db.collection(KNOWLEDGE_JOBS_COLLECTION).document(file_id).set(
            {"fileId": file_id, "filename": "uncertain.pdf", "state": "indexing", "attempts": 0}
        )
        sender = FakeFcmSender(RuntimeError("ambiguous transport result"))
        service = NotificationService(self.repository, sender)
        first = await process_knowledge_notification_job(
            file_id,
            db=self.db,
            client=FakeIndexingClient(["completed"], sender),
            vector_store_id="vs-emulator-only",
            notification_service=service,
        )
        second = await process_knowledge_notification_job(
            file_id,
            db=self.db,
            client=FakeIndexingClient(["completed"], sender),
            vector_store_id="vs-emulator-only",
            notification_service=service,
        )
        self.assertEqual(first, "delivery_unknown")
        self.assertEqual(second, "delivery_unknown")
        self.assertEqual(len(sender.calls), 1)

if __name__ == "__main__":
    unittest.main()
