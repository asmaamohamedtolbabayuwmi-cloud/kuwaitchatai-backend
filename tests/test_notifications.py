import asyncio
import os
import unittest
from datetime import datetime, timedelta, timezone
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from google.api_core.exceptions import AlreadyExists

from app.services.notification_service import (
    AnnouncementDeliveryError,
    AnnouncementIdempotencyConflict,
    AnnouncementStorageError,
    ConfirmedPreSendNotificationError,
    DeliveryResult,
    NotificationRepository,
    NotificationService,
    viewer_identity,
    version_topic,
)
from app.services.knowledge_file_notifications import (
    _reconcile_sending_after_lease,
    _renew_lease,
    _update_owned_job,
    create_knowledge_notification_job,
    recover_pending_knowledge_notification_jobs,
    process_knowledge_notification_job,
    resume_pending_knowledge_notification_jobs,
)
from app.auth import verify_admin
from app.routers.notifications import (
    AnnouncementRequest,
    AnnouncementViewRequest,
    AppUpdateRequest,
    create_announcement as create_announcement_route,
    router,
)
from pydantic import ValidationError
from fastapi import HTTPException, Response, UploadFile


class FakeAnnouncementRepository:
    def __init__(self):
        self.records = {}
        self.delivery = []
        self.keys = {}

    async def get_or_create_announcement(
        self, title, body, admin_uid, idempotency_key
    ):
        existing = self.keys.get(idempotency_key)
        if existing:
            record = self.records[existing]
            if record["title"] != title or record["body"] != body:
                raise AnnouncementIdempotencyConflict("different content")
            return {
                "id": existing,
                "created": False,
                "deliveryStatus": record["status"],
            }
        announcement_id = f"announcement-{len(self.records) + 1}"
        self.records[announcement_id] = {
            "title": title,
            "body": body,
            "admin": admin_uid,
            "status": "pending",
        }
        self.keys[idempotency_key] = announcement_id
        return {
            "id": announcement_id,
            "created": True,
            "deliveryStatus": "pending",
        }

    async def claim_announcement_delivery(self, announcement_id):
        record = self.records[announcement_id]
        if record["status"] in {"sent", "delivery_unknown"}:
            return record["status"]
        record["status"] = "dispatching"
        return "dispatching"

    async def mark_delivery(self, announcement_id, **values):
        self.delivery.append((announcement_id, values))
        self.records[announcement_id]["status"] = values["status"]


class FakeSender:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    async def send_topic(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return DeliveryResult(message_id="message-1", topic=kwargs["topic"])


class NotificationServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_announcement_is_stored_and_targets_general_topic(self):
        repository = FakeAnnouncementRepository()
        sender = FakeSender()
        result = await NotificationService(repository, sender).create_announcement(
            " General  notice ",
            " New  information ",
            "admin-1",
            "request-key-0001",
        )

        self.assertEqual(result["topic"], "all_users")
        self.assertEqual(sender.calls[0]["topic"], "all_users")
        self.assertEqual(sender.calls[0]["data"]["type"], "announcement")
        self.assertEqual(repository.delivery[-1][1]["status"], "sent")

    async def test_ambiguous_send_is_quarantined_and_idempotent_retry_does_not_send(self):
        repository = FakeAnnouncementRepository()
        sender = FakeSender(RuntimeError("FCM unavailable"))
        service = NotificationService(repository, sender)
        with self.assertRaises(AnnouncementDeliveryError) as first:
            await service.create_announcement(
                "Notice", "Body", "admin-1", "request-key-0002"
            )
        self.assertEqual(first.exception.delivery_status, "delivery_unknown")
        self.assertIn("announcement-1", repository.records)
        self.assertEqual(repository.delivery[-1][1]["status"], "delivery_unknown")

        sender.error = None
        with self.assertRaises(AnnouncementDeliveryError) as retry:
            await service.create_announcement(
                "Notice", "Body", "admin-1", "request-key-0002"
            )
        self.assertEqual(retry.exception.delivery_status, "delivery_unknown")
        self.assertEqual(len(sender.calls), 1)

    async def test_confirmed_pre_dispatch_failure_can_retry_same_announcement(self):
        repository = FakeAnnouncementRepository()
        sender = FakeSender(ConfirmedPreSendNotificationError("invalid payload"))
        service = NotificationService(repository, sender)
        with self.assertRaises(AnnouncementDeliveryError) as first:
            await service.create_announcement(
                "Notice", "Body", "admin-1", "request-key-0003"
            )
        self.assertEqual(
            first.exception.delivery_status, "confirmed_pre_dispatch_failure"
        )
        self.assertEqual(
            repository.delivery[-1][1]["status"],
            "confirmed_pre_dispatch_failure",
        )

        sender.error = None
        result = await service.create_announcement(
            "Notice", "Body", "admin-1", "request-key-0003"
        )
        self.assertEqual(result["id"], "announcement-1")
        self.assertEqual(result["deliveryStatus"], "sent")
        self.assertEqual(len(repository.records), 1)
        self.assertEqual(len(sender.calls), 2)

    async def test_sent_idempotent_retry_returns_existing_without_dispatch(self):
        repository = FakeAnnouncementRepository()
        sender = FakeSender()
        service = NotificationService(repository, sender)
        first = await service.create_announcement(
            "Notice", "Body", "admin-1", "request-key-0004"
        )
        retry = await service.create_announcement(
            "Notice", "Body", "admin-1", "request-key-0004"
        )
        self.assertEqual(first["id"], retry["id"])
        self.assertFalse(retry["created"])
        self.assertEqual(len(sender.calls), 1)

    async def test_storage_failure_does_not_claim_announcement_was_saved(self):
        class BrokenRepository(FakeAnnouncementRepository):
            async def get_or_create_announcement(self, *args, **kwargs):
                raise RuntimeError("Firestore unavailable")

        with self.assertRaises(AnnouncementStorageError):
            await NotificationService(
                BrokenRepository(), FakeSender()
            ).create_announcement(
                "Notice", "Body", "admin-1", "request-key-0005"
            )

    async def test_app_update_targets_installed_version_topic(self):
        sender = FakeSender()
        result = await NotificationService(FakeAnnouncementRepository(), sender).send_app_update(
            title="Update",
            body="Available",
            target_version="1.2.0",
            new_version="1.3.0",
            force=True,
        )
        self.assertEqual(result["topic"], "version_1_2_0")
        self.assertEqual(sender.calls[0]["data"]["force"], "true")
        self.assertEqual(sender.calls[0]["data"]["type"], "app_update")

    async def test_app_update_requires_newer_version(self):
        service = NotificationService(FakeAnnouncementRepository(), FakeSender())
        with self.assertRaisesRegex(ValueError, "greater"):
            await service.send_app_update(
                title="Update",
                body="Available",
                target_version="1.3.0",
                new_version="1.2.9",
                force=False,
            )
        with self.assertRaisesRegex(ValueError, "greater"):
            await service.send_app_update(
                title="Update",
                body="Available",
                target_version="1.2",
                new_version="1.2.0",
                force=False,
            )

    def test_validation_and_private_viewer_hashing(self):
        self.assertEqual(version_topic("1.2.0"), "version_1_2_0")
        with self.assertRaises(ValueError):
            version_topic("latest")
        with patch.dict(os.environ, {"ANALYTICS_ID_PEPPER": "p" * 32}):
            first = viewer_identity(installation_id="a" * 24, uid=None)
            signed_in = viewer_identity(installation_id="a" * 24, uid="uid-1")
        self.assertEqual(first, signed_in)
        self.assertNotIn("uid-1", first)

    def test_admin_routes_use_admin_dependency_and_models_validate_types(self):
        admin_paths = {
            "/notifications/announcements",
            "/notifications/app-update",
            "/notifications/announcements/stats",
        }
        for route in router.routes:
            if route.path in admin_paths:
                dependencies = {dependency.call for dependency in route.dependant.dependencies}
                self.assertIn(verify_admin, dependencies)
        with self.assertRaises(ValidationError):
            AnnouncementRequest(title="", body="Body")
        with self.assertRaises(ValidationError):
            AnnouncementViewRequest(installationId="not valid with spaces")
        request = AppUpdateRequest(
            title="Update",
            body="Available",
            target_version="1.2.0",
            version="1.3.0",
        )
        self.assertFalse(request.force)


class AnnouncementRouteErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_storage_failure_reports_creation_as_unknown(self):
        service = SimpleNamespace(
            create_announcement=AsyncMock(
                side_effect=AnnouncementStorageError(
                    "Announcement creation could not be confirmed.",
                    idempotency_key="request-key-1001",
                )
            )
        )
        with patch(
            "app.routers.notifications.NotificationService",
            return_value=service,
        ):
            with self.assertRaises(HTTPException) as caught:
                await create_announcement_route(
                    AnnouncementRequest(title="Notice", body="Body"),
                    Response(),
                    "admin-1",
                    "request-key-1001",
                )
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(
            caught.exception.detail["code"], "announcement_creation_unknown"
        )
        self.assertIsNone(caught.exception.detail["announcementCreated"])
        self.assertFalse(caught.exception.detail["pushDispatched"])
        self.assertIsNone(caught.exception.detail["deliveryStatus"])

    async def test_ambiguous_delivery_response_includes_saved_announcement(self):
        service = SimpleNamespace(
            create_announcement=AsyncMock(
                side_effect=AnnouncementDeliveryError(
                    announcement_id="announcement-1",
                    idempotency_key="request-key-1002",
                    delivery_status="delivery_unknown",
                    message="FCM outcome unknown",
                )
            )
        )
        with patch(
            "app.routers.notifications.NotificationService",
            return_value=service,
        ):
            with self.assertRaises(HTTPException) as caught:
                await create_announcement_route(
                    AnnouncementRequest(title="Notice", body="Body"),
                    Response(),
                    "admin-1",
                    "request-key-1002",
                )
        self.assertEqual(caught.exception.status_code, 502)
        self.assertEqual(caught.exception.detail["code"], "push_delivery_unknown")
        self.assertEqual(caught.exception.detail["announcementId"], "announcement-1")


class Snapshot:
    def __init__(self, exists, data=None, snapshot_id="announcement-1"):
        self.exists = exists
        self._data = data or {}
        self.id = snapshot_id

    def to_dict(self):
        return dict(self._data)


class AnnouncementQuery:
    def __init__(self, snapshots):
        self.snapshots = snapshots
        self.result_limit = None

    def where(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        self.snapshots.sort(
            key=lambda item: item.to_dict()["createdAt"], reverse=True
        )
        return self

    def limit(self, value):
        self.result_limit = value
        return self

    async def stream(self):
        for item in self.snapshots[: self.result_limit]:
            yield item


class LatestDb:
    def __init__(self, snapshots):
        self.query = AnnouncementQuery(snapshots)

    def collection(self, name):
        return self.query


class LatestAnnouncementTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_exactly_three_newest_manual_announcements(self):
        from datetime import datetime, timezone

        snapshots = [
            Snapshot(
                True,
                {
                    "type": "announcement",
                    "title": f"Title {index}",
                    "body": "Body",
                    "createdAt": datetime(2026, 1, index + 1, tzinfo=timezone.utc),
                },
                f"id-{index}",
            )
            for index in range(5)
        ]
        results = await NotificationRepository(LatestDb(snapshots)).latest_announcements(3)
        self.assertEqual([item["id"] for item in results], ["id-4", "id-3", "id-2"])


class ViewRef:
    def __init__(self, state, key):
        self.state = state
        self.key = key

    async def get(self, transaction=None):
        data = self.state["views"].get(self.key)
        return Snapshot(data is not None, data)


class AnnouncementRef:
    def __init__(self, state):
        self.state = state

    async def get(self, transaction=None):
        return Snapshot(True, self.state["announcement"])

    def collection(self, name):
        return SimpleNamespace(document=lambda key: ViewRef(self.state, key))


class ViewTransaction:
    def __init__(self, state):
        self.state = state

    def set(self, ref, data):
        self.state["views"][ref.key] = dict(data)

    def update(self, ref, data):
        if isinstance(ref, ViewRef):
            self.state["views"][ref.key].update(data)
        elif "viewCount" in data:
            self.state["announcement"]["viewCount"] += 1


class ViewDb:
    def __init__(self):
        self.state = {
            "announcement": {"type": "announcement", "viewCount": 0},
            "views": {},
        }
        self.lock = asyncio.Lock()

    def collection(self, name):
        return SimpleNamespace(document=lambda key: AnnouncementRef(self.state))

    def transaction(self):
        return ViewTransaction(self.state)


class ViewDeduplicationTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_concurrent_views_increment_once(self):
        db = ViewDb()

        def transactional(function):
            async def wrapper(transaction):
                async with db.lock:
                    return await function(transaction)

            return wrapper

        repository = NotificationRepository(db)
        with patch(
            "app.services.notification_service.firestore_async.async_transactional",
            transactional,
        ):
            results = await asyncio.gather(
                repository.record_view("announcement-1", "viewer", authenticated=False),
                repository.record_view("announcement-1", "viewer", authenticated=True),
            )
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(db.state["announcement"]["viewCount"], 1)
        self.assertTrue(db.state["views"]["viewer"]["authenticatedSeen"])


class JobRef:
    def __init__(self, state):
        self.state = state
        self.before_transaction_get = None
        self.transaction_get_calls = 0
        self.before_transaction_get_on_call = {}

    async def get(self, transaction=None):
        if transaction is not None:
            self.transaction_get_calls += 1
            callback = self.before_transaction_get_on_call.pop(
                self.transaction_get_calls, None
            )
            if callback is not None:
                callback()
        if transaction is not None and self.before_transaction_get is not None:
            callback = self.before_transaction_get
            self.before_transaction_get = None
            callback()
        return Snapshot(True, self.state)

    async def update(self, values):
        self.state.update(values)


class JobTransaction:
    def __init__(self, db=None):
        self.db = db

    def update(self, ref, values):
        if self.db is not None and self.db.transaction_update_hook is not None:
            self.db.transaction_update_hook(values)
        ref.state.update(values)


class JobDb:
    def __init__(self, state=None):
        self.state = state or {
            "state": "indexing",
            "filename": "guide.pdf",
            "attempts": 0,
        }
        self.ref = JobRef(self.state)
        self.lock = asyncio.Lock()
        self.transaction_update_hook = None

    def collection(self, name):
        return SimpleNamespace(document=lambda key: self.ref)

    def transaction(self):
        return JobTransaction(self)


class VectorClient:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.calls = 0
        self.vector_stores = SimpleNamespace(
            files=SimpleNamespace(retrieve=self.retrieve)
        )

    async def retrieve(self, **kwargs):
        index = min(self.calls, len(self.statuses) - 1)
        self.calls += 1
        result = self.statuses[index]
        if isinstance(result, Exception):
            raise result
        return SimpleNamespace(status=result)


class KnowledgeNotifier:
    def __init__(self, error=None):
        self.error = error
        self.calls = 0

    async def send_knowledge_file(self, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return DeliveryResult("message-knowledge", "all_users")


class KnowledgeJobTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, statuses, attempts=3, notifier=None, state=None):
        db = JobDb(state)
        notifier = notifier or KnowledgeNotifier()
        with patch(
            "app.services.knowledge_file_notifications.firestore_async.async_transactional",
            lambda function: function,
        ):
            result = await process_knowledge_notification_job(
                "file-1",
                max_attempts=attempts,
                poll_seconds=0,
                delay=lambda _: asyncio.sleep(0),
                db=db,
                client=VectorClient(statuses),
                vector_store_id="vs-test",
                notification_service=notifier,
            )
        return result, db, notifier

    async def test_in_progress_retries_then_completed_sends_once(self):
        result, db, notifier = await self._run(["in_progress", "completed"])
        self.assertEqual(result, "sent")
        self.assertEqual(db.state["state"], "sent")
        self.assertEqual(notifier.calls, 1)

    async def test_failed_index_never_sends(self):
        result, db, notifier = await self._run(["failed"])
        self.assertEqual(result, "indexing_failed")
        self.assertEqual(notifier.calls, 0)

    async def test_timeout_never_sends(self):
        result, db, notifier = await self._run(["in_progress"], attempts=2)
        self.assertEqual(result, "timed_out")
        self.assertEqual(notifier.calls, 0)

    async def test_notification_failure_does_not_invalidate_indexed_file(self):
        notifier = KnowledgeNotifier(RuntimeError("FCM down"))
        result, db, _ = await self._run(["completed"], notifier=notifier)
        self.assertEqual(result, "delivery_unknown")
        self.assertEqual(db.state["state"], "delivery_unknown")
        self.assertEqual(db.state["indexingStatus"], "completed")

    async def test_confirmed_pre_send_failure_is_retryable(self):
        notifier = KnowledgeNotifier(
            ConfirmedPreSendNotificationError("invalid local payload")
        )
        result, db, _ = await self._run(["completed"], notifier=notifier)
        self.assertEqual(result, "notification_failed")
        self.assertEqual(
            db.state["deliveryDisposition"], "confirmed_pre_send_failure"
        )

    async def test_confirmed_pre_send_failure_can_retry_after_restart(self):
        state = {
            "state": "notification_failed",
            "filename": "guide.pdf",
            "attempts": 60,
            "indexingStatus": "completed",
            "deliveryDisposition": "confirmed_pre_send_failure",
        }
        result, db, notifier = await self._run(
            [RuntimeError("OpenAI should not be called")],
            attempts=60,
            state=state,
        )
        self.assertEqual(result, "sent")
        self.assertEqual(db.state["state"], "sent")
        self.assertEqual(notifier.calls, 1)

    async def test_missing_openai_file_is_permanent_and_never_sends(self):
        missing = RuntimeError("file was deleted")
        missing.status_code = 404
        result, db, notifier = await self._run([missing])
        self.assertEqual(result, "source_missing")
        self.assertEqual(db.state["state"], "source_missing")
        self.assertEqual(notifier.calls, 0)

    async def test_transient_openai_error_retries(self):
        unavailable = RuntimeError("temporarily unavailable")
        unavailable.status_code = 503
        result, db, notifier = await self._run([unavailable, "completed"])
        self.assertEqual(result, "sent")
        self.assertEqual(db.state["attempts"], 2)
        self.assertEqual(notifier.calls, 1)

    async def test_parallel_workers_use_one_lease_and_send_once(self):
        db = JobDb()
        notifier = KnowledgeNotifier()

        def transactional(function):
            async def wrapper(transaction):
                async with db.lock:
                    return await function(transaction)

            return wrapper

        async def run(owner):
            return await process_knowledge_notification_job(
                "file-1",
                max_attempts=2,
                poll_seconds=0,
                delay=lambda _: asyncio.sleep(0),
                db=db,
                client=VectorClient(["completed"]),
                vector_store_id="vs-test",
                notification_service=notifier,
                owner_id=owner,
            )

        with patch(
            "app.services.knowledge_file_notifications.firestore_async.async_transactional",
            transactional,
        ):
            results = await asyncio.gather(run("replica-a"), run("replica-b"))

        self.assertEqual(notifier.calls, 1)
        self.assertIn("sent", results)
        self.assertTrue(any(result in {"leased", "sent"} for result in results))

    async def test_send_claim_rejects_state_changed_after_index_update(self):
        now = datetime(2026, 10, 19, tzinfo=timezone.utc)
        db = JobDb()
        notifier = KnowledgeNotifier()

        def become_terminal_before_claim():
            db.state["state"] = "timed_out"

        db.ref.before_transaction_get_on_call[4] = become_terminal_before_claim
        with patch(
            "app.services.knowledge_file_notifications.firestore_async.async_transactional",
            lambda function: function,
        ):
            result = await process_knowledge_notification_job(
                "file-1",
                db=db,
                client=VectorClient(["completed"]),
                vector_store_id="vs-test",
                notification_service=notifier,
                owner_id="worker-1",
                clock=lambda: now,
            )

        self.assertEqual(result, "timed_out")
        self.assertEqual(notifier.calls, 0)
        self.assertEqual(db.state["state"], "timed_out")

    async def test_transaction_retry_rechecks_lease_clock(self):
        now = datetime(2026, 10, 19, tzinfo=timezone.utc)
        current = [now]
        state = {
            "state": "indexing",
            "leaseOwner": "worker-1",
            "leaseExpiresAt": now + timedelta(seconds=1),
        }
        db = JobDb(state)

        def retrying_transactional(function):
            async def wrapper(transaction):
                class DiscardingTransaction:
                    def update(self, ref, values):
                        return None

                await function(DiscardingTransaction())
                current[0] = now + timedelta(seconds=2)
                return await function(transaction)

            return wrapper

        with patch(
            "app.services.knowledge_file_notifications.firestore_async.async_transactional",
            retrying_transactional,
        ):
            renewed = await _renew_lease(
                db.ref,
                db,
                owner="worker-1",
                clock=lambda: current[0],
                lease_seconds=1,
            )

        self.assertFalse(renewed)
        self.assertEqual(state["leaseExpiresAt"], now + timedelta(seconds=1))

    async def test_owned_update_retry_rechecks_lease_clock(self):
        now = datetime(2026, 10, 19, tzinfo=timezone.utc)
        current = [now]
        state = {
            "state": "indexing",
            "leaseOwner": "worker-1",
            "leaseExpiresAt": now + timedelta(seconds=1),
        }
        db = JobDb(state)

        def retrying_transactional(function):
            async def wrapper(transaction):
                class DiscardingTransaction:
                    def update(self, ref, values):
                        return None

                await function(DiscardingTransaction())
                current[0] = now + timedelta(seconds=2)
                return await function(transaction)

            return wrapper

        with patch(
            "app.services.knowledge_file_notifications.firestore_async.async_transactional",
            retrying_transactional,
        ):
            result = await _update_owned_job(
                db.ref,
                db,
                owner="worker-1",
                clock=lambda: current[0],
                allowed_states={"indexing"},
                values={"state": "source_error"},
            )

        self.assertEqual(result, "leased")
        self.assertEqual(state["state"], "indexing")

    async def test_firestore_write_failure_is_contained(self):
        db = JobDb()
        db.transaction_update_hook = lambda _values: (_ for _ in ()).throw(
            RuntimeError("Firestore unavailable")
        )
        with patch(
            "app.services.knowledge_file_notifications.firestore_async.async_transactional",
            lambda function: function,
        ):
            result = await process_knowledge_notification_job(
                "file-1",
                db=db,
                client=VectorClient(["completed"]),
                vector_store_id="vs-test",
                notification_service=KnowledgeNotifier(),
            )
        self.assertEqual(result, "worker_error")

    async def test_fcm_acceptance_followed_by_write_failure_is_unknown(self):
        db = JobDb()
        failed = False

        def fail_sent_write(values):
            nonlocal failed
            if values.get("state") == "sent":
                failed = True
                raise RuntimeError("Firestore unavailable after FCM acceptance")

        db.transaction_update_hook = fail_sent_write
        notifier = KnowledgeNotifier()
        with patch(
            "app.services.knowledge_file_notifications.firestore_async.async_transactional",
            lambda function: function,
        ):
            result = await process_knowledge_notification_job(
                "file-1",
                db=db,
                client=VectorClient(["completed"]),
                vector_store_id="vs-test",
                notification_service=notifier,
            )
        self.assertEqual(result, "delivery_unknown")
        self.assertEqual(db.state["state"], "delivery_unknown")
        self.assertEqual(
            db.state["deliveryDisposition"], "accepted_but_not_persisted"
        )
        self.assertEqual(notifier.calls, 1)

    async def test_sent_job_is_deduplicated(self):
        state = {"state": "sent", "filename": "guide.pdf", "attempts": 1}
        result, _, notifier = await self._run(["completed"], state=state)
        self.assertEqual(result, "sent")
        self.assertEqual(notifier.calls, 0)

    async def test_expired_lease_after_slow_lookup_never_dispatches(self):
        now = datetime(2026, 10, 19, tzinfo=timezone.utc)
        current = [now]

        class SlowVectorClient(VectorClient):
            async def retrieve(self, **kwargs):
                current[0] = now + timedelta(seconds=2)
                return await super().retrieve(**kwargs)

        db = JobDb()
        notifier = KnowledgeNotifier()
        with patch(
            "app.services.knowledge_file_notifications.firestore_async.async_transactional",
            lambda function: function,
        ):
            result = await process_knowledge_notification_job(
                "file-1",
                db=db,
                client=SlowVectorClient(["completed"]),
                vector_store_id="vs-test",
                notification_service=notifier,
                owner_id="slow-worker",
                lease_seconds=1,
                clock=lambda: current[0],
            )

        self.assertEqual(result, "leased")
        self.assertEqual(notifier.calls, 0)
        self.assertEqual(db.state["state"], "indexing")

    async def test_expired_lease_is_not_renewed_after_poll_delay(self):
        now = datetime(2026, 10, 19, tzinfo=timezone.utc)
        current = [now]

        async def slow_delay(_seconds):
            current[0] = now + timedelta(seconds=2)

        db = JobDb()
        notifier = KnowledgeNotifier()
        with patch(
            "app.services.knowledge_file_notifications.firestore_async.async_transactional",
            lambda function: function,
        ):
            result = await process_knowledge_notification_job(
                "file-1",
                db=db,
                client=VectorClient(["in_progress", "completed"]),
                vector_store_id="vs-test",
                notification_service=notifier,
                owner_id="slow-worker",
                lease_seconds=1,
                poll_seconds=1,
                delay=slow_delay,
                clock=lambda: current[0],
            )

        self.assertEqual(result, "leased")
        self.assertEqual(notifier.calls, 0)
        self.assertEqual(db.state["state"], "indexing")


class CreationRef:
    def __init__(self):
        self.state = None
        self.create_calls = 0

    async def create(self, values):
        self.create_calls += 1
        await asyncio.sleep(0)
        if self.state is not None:
            raise AlreadyExists("job already exists")
        self.state = dict(values)

    async def get(self):
        return Snapshot(self.state is not None, self.state or {})


class CreationDb:
    def __init__(self):
        self.ref = CreationRef()

    def collection(self, name):
        return SimpleNamespace(document=lambda key: self.ref)


class KnowledgeJobCreationTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_identical_creation_is_idempotent(self):
        db = CreationDb()
        with patch(
            "app.services.knowledge_file_notifications.firestore_async.client",
            return_value=db,
        ):
            results = await asyncio.gather(
                create_knowledge_notification_job("file-1", "guide.pdf"),
                create_knowledge_notification_job("file-1", "guide.pdf"),
                return_exceptions=True,
            )

        self.assertEqual(results, [None, None])
        self.assertEqual(db.ref.state["fileId"], "file-1")
        self.assertEqual(db.ref.create_calls, 2)

    async def test_already_existing_different_job_is_not_accepted(self):
        db = CreationDb()
        db.ref.state = {"fileId": "different-file", "state": "indexing"}

        with (
            patch(
                "app.services.knowledge_file_notifications.firestore_async.client",
                return_value=db,
            ),
            self.assertRaises(AlreadyExists),
        ):
            await create_knowledge_notification_job("file-1", "guide.pdf")


class RecoverySnapshot(Snapshot):
    def __init__(self, snapshot_id, state):
        super().__init__(True, state, snapshot_id)
        self.reference = JobRef(state)
        self._data = self.reference.state


class RecoveryQuery:
    def __init__(self, snapshots, error=None):
        self.snapshots = snapshots
        self.error = error

    def where(self, *args, **kwargs):
        return self

    async def stream(self):
        if self.error:
            raise self.error
        for snapshot in self.snapshots:
            yield snapshot


class RecoveryDb:
    def __init__(self, snapshots, error=None):
        self.query = RecoveryQuery(snapshots, error)

    def collection(self, name):
        return self.query

    def transaction(self):
        return JobTransaction()


class CapturingTaskTracker:
    def __init__(self):
        self.count = 0

    def create_task(self, coroutine):
        self.count += 1
        coroutine.close()


class RunningTaskTracker:
    def __init__(self):
        self.tasks = []

    def create_task(self, coroutine):
        self.tasks.append(asyncio.create_task(coroutine))


class KnowledgeRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_multiple_replica_recovery_keeps_ambiguous_delivery_quarantined(self):
        shared_db = JobDb()
        notifier = KnowledgeNotifier(RuntimeError("ambiguous transport result"))
        tracker_a = RunningTaskTracker()
        tracker_b = RunningTaskTracker()
        real_process = process_knowledge_notification_job

        async def recovered_process(file_id):
            return await real_process(
                file_id,
                db=shared_db,
                client=VectorClient(["completed"]),
                vector_store_id="vs-test",
                notification_service=notifier,
            )

        replica_a = RecoveryDb([RecoverySnapshot("file-1", shared_db.state)])
        replica_b = RecoveryDb([RecoverySnapshot("file-1", shared_db.state)])

        def transactional(function):
            async def wrapper(transaction):
                async with shared_db.lock:
                    return await function(transaction)

            return wrapper

        with (
            patch(
                "app.services.knowledge_file_notifications.firestore_async.async_transactional",
                transactional,
            ),
            patch(
                "app.services.knowledge_file_notifications.process_knowledge_notification_job",
                recovered_process,
            ),
        ):
            await asyncio.gather(
                resume_pending_knowledge_notification_jobs(
                    db=replica_a, task_tracker=tracker_a
                ),
                resume_pending_knowledge_notification_jobs(
                    db=replica_b, task_tracker=tracker_b
                ),
            )
            results = await asyncio.gather(*(tracker_a.tasks + tracker_b.tasks))

        self.assertEqual(notifier.calls, 1)
        self.assertEqual(shared_db.state["state"], "delivery_unknown")
        self.assertTrue(
            all(result in {"delivery_unknown", "leased"} for result in results)
        )

    async def test_stale_recovery_does_not_overwrite_concurrent_sent_state(self):
        now = datetime(2026, 10, 19, tzinfo=timezone.utc)
        stale_send = RecoverySnapshot(
            "stale-send",
            {
                "state": "sending",
                "leaseOwner": "old-worker",
                "leaseExpiresAt": now - timedelta(seconds=1),
            },
        )
        original_update = stale_send.reference.update

        async def sent_before_recovery_write(values):
            stale_send.reference.state.update(
                {"state": "sent", "leaseOwner": None, "leaseExpiresAt": None}
            )
            await original_update(values)

        stale_send.reference.update = sent_before_recovery_write
        stale_send.reference.before_transaction_get = lambda: stale_send.reference.state.update(
            {"state": "sent", "leaseOwner": None, "leaseExpiresAt": None}
        )
        db = RecoveryDb([stale_send])
        with patch(
            "app.services.knowledge_file_notifications.firestore_async.async_transactional",
            lambda function: function,
        ):
            await resume_pending_knowledge_notification_jobs(db=db, clock=lambda: now)

        self.assertEqual(stale_send.reference.state["state"], "sent")

    async def test_deferred_reconciliation_does_not_overwrite_new_lease(self):
        now = datetime(2026, 10, 19, tzinfo=timezone.utc)
        ref = JobRef(
            {
                "state": "sending",
                "leaseOwner": "old-worker",
                "leaseExpiresAt": now - timedelta(seconds=1),
            }
        )
        original_update = ref.update

        async def renewed_before_reconciliation_write(values):
            ref.state.update(
                {
                    "state": "sending",
                    "leaseOwner": "new-worker",
                    "leaseExpiresAt": now + timedelta(minutes=5),
                }
            )
            await original_update(values)

        ref.update = renewed_before_reconciliation_write
        ref.before_transaction_get = lambda: ref.state.update(
            {
                "state": "sending",
                "leaseOwner": "new-worker",
                "leaseExpiresAt": now + timedelta(minutes=5),
            }
        )
        db = JobDb(ref.state)
        db.ref = ref
        with patch(
            "app.services.knowledge_file_notifications.firestore_async.async_transactional",
            lambda function: function,
        ):
            await _reconcile_sending_after_lease(
                "file-1",
                wait_seconds=0,
                db=db,
                delay=lambda _: asyncio.sleep(0),
                clock=lambda: now,
            )

        self.assertEqual(ref.state["state"], "sending")
        self.assertEqual(ref.state["leaseOwner"], "new-worker")
        self.assertGreater(ref.state["leaseExpiresAt"], now)

    async def test_railway_restart_quarantines_stale_send_and_resumes_polling(self):
        now = datetime(2026, 10, 19, tzinfo=timezone.utc)
        stale_send = RecoverySnapshot(
            "stale-send",
            {
                "state": "sending",
                "leaseExpiresAt": now - timedelta(seconds=1),
            },
        )
        pending = RecoverySnapshot("pending", {"state": "indexing", "attempts": 2})
        tracker = CapturingTaskTracker()

        with patch(
            "app.services.knowledge_file_notifications.firestore_async.async_transactional",
            lambda function: function,
        ):
            count = await resume_pending_knowledge_notification_jobs(
                db=RecoveryDb([stale_send, pending]),
                task_tracker=tracker,
                clock=lambda: now,
            )

        self.assertEqual(count, 1)
        self.assertEqual(tracker.count, 1)
        self.assertEqual(stale_send.reference.state["state"], "delivery_unknown")
        self.assertIn("operatorNote", stale_send.reference.state)

    async def test_delivery_unknown_is_never_rescheduled(self):
        unknown = RecoverySnapshot("unknown", {"state": "delivery_unknown"})
        tracker = CapturingTaskTracker()
        count = await resume_pending_knowledge_notification_jobs(
            db=RecoveryDb([unknown]), task_tracker=tracker
        )
        self.assertEqual(count, 0)
        self.assertEqual(tracker.count, 0)

    async def test_recovery_failure_retries_then_succeeds(self):
        calls = 0
        waits = []

        async def resume():
            nonlocal calls
            calls += 1
            if calls < 3:
                raise RuntimeError("Firestore unavailable")
            return 2

        async def no_wait(seconds):
            waits.append(seconds)

        result = await recover_pending_knowledge_notification_jobs(
            max_attempts=3,
            base_delay_seconds=2,
            delay=no_wait,
            resume=resume,
        )
        self.assertEqual(result, 2)
        self.assertEqual(calls, 3)
        self.assertEqual(waits, [2, 4])

    async def test_exhausted_recovery_never_raises(self):
        async def fail():
            raise RuntimeError("Firestore unavailable")

        result = await recover_pending_knowledge_notification_jobs(
            max_attempts=2,
            base_delay_seconds=0,
            delay=lambda _: asyncio.sleep(0),
            resume=fail,
        )
        self.assertEqual(result, 0)


class UploadResilienceTests(unittest.IsolatedAsyncioTestCase):
    async def test_notification_job_failure_does_not_fail_successful_upload(self):
        from app.routers.upload import upload

        async def uploaded(*args, **kwargs):
            return SimpleNamespace(
                file_id="file-1",
                filename="guide.pdf",
                status="in_progress",
            )

        async def scheduling_failed(*args, **kwargs):
            raise RuntimeError("Firestore unavailable")

        file = UploadFile(filename="guide.pdf", file=BytesIO(b"pdf content"))
        with (
            patch("app.routers.upload.upload_file_to_vector_store", uploaded),
            patch(
                "app.routers.upload.create_knowledge_notification_job",
                scheduling_failed,
            ),
        ):
            response = await upload(file=file, uid="admin-1")

        self.assertEqual(response["status"], "success")
        self.assertEqual(response["file_id"], "file-1")
        self.assertEqual(response["notification_status"], "scheduling_failed")


class StartupResilienceTests(unittest.IsolatedAsyncioTestCase):
    async def test_recovery_scheduling_failure_does_not_prevent_startup(self):
        import main

        class FailingTracker:
            def create_task(self, coroutine):
                coroutine.close()
                raise RuntimeError("task scheduler unavailable")

            async def wait_all(self, timeout):
                return None

        config = SimpleNamespace(
            OPENAI_MODEL="test",
            OPENAI_SCHEDULE_MODEL="test",
            OPENAI_VECTOR_STORE_ID="vs-test",
            HISTORY_TOKEN_BUDGET=100,
        )
        entered = False
        with (
            patch("app.config.get_config", return_value=config),
            patch("main.init_firebase"),
            patch("main.background_tasks", FailingTracker()),
        ):
            async with main.lifespan(SimpleNamespace()):
                entered = True
        self.assertTrue(entered)

if __name__ == "__main__":
    unittest.main()
