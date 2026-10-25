"""Notification creation, delivery, history, and view analytics.

FCM payload contract (all values are strings):

* Common: ``type`` (announcement|knowledge_file|app_update), ``event_id``.
* announcement: ``announcement_id``.
* knowledge_file: ``file_id``, ``filename``.
* app_update: ``version``, ``target_version``, ``force`` (true|false).

Legacy app-update clients remain compatible because ``version`` and ``force``
retain their existing meanings. Ordinary notifications use one FCM condition
covering ``all_users`` and the legacy iOS ``version_1_1_1`` topic; app updates
continue to use ``version_X_Y_Z`` topics.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from firebase_admin import firestore, firestore_async, messaging
from ..local_notification_test import (
    deliver_local_test_message,
    enabled as local_notification_test_enabled,
)

logger = logging.getLogger(__name__)

GENERAL_TOPIC = "all_users"
LEGACY_IOS_TOPIC = "version_1_1_1"
GENERAL_AUDIENCE_CONDITION = (
    f"'{GENERAL_TOPIC}' in topics || '{LEGACY_IOS_TOPIC}' in topics"
)
ANNOUNCEMENTS_COLLECTION = "announcements"
KNOWLEDGE_JOBS_COLLECTION = "knowledge_file_notification_jobs"
_VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+){1,3}$")
_INSTALLATION_RE = re.compile(r"^[A-Za-z0-9_-]{20,128}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9._:-]{16,128}$")
_DISPATCH_STALE_AFTER = timedelta(minutes=5)
_KNOWLEDGE_FILE_EXTENSIONS = frozenset(
    {".pdf", ".txt", ".csv", ".md", ".docx", ".json"}
)


class AnnouncementStorageError(RuntimeError):
    """Firestore could not confirm the announcement creation/read outcome."""

    def __init__(self, message: str, *, idempotency_key: str | None = None) -> None:
        super().__init__(message)
        self.idempotency_key = idempotency_key


class AnnouncementIdempotencyConflict(ValueError):
    """An idempotency key was reused with different announcement content."""


class AnnouncementDeliveryError(RuntimeError):
    def __init__(
        self,
        *,
        announcement_id: str,
        idempotency_key: str,
        delivery_status: str,
        message: str,
    ) -> None:
        super().__init__(message)
        self.announcement_id = announcement_id
        self.idempotency_key = idempotency_key
        self.delivery_status = delivery_status


def version_topic(version: str) -> str:
    value, _ = parse_version(version)
    return f"version_{value.replace('.', '_')}"


def parse_version(version: str) -> tuple[str, tuple[int, ...]]:
    value = version.strip()
    if not _VERSION_RE.fullmatch(value):
        raise ValueError("Version must contain 2 to 4 numeric components.")
    return value, tuple(int(part) for part in value.split("."))


def validate_version_upgrade(target_version: str, new_version: str) -> None:
    _, target = parse_version(target_version)
    _, candidate = parse_version(new_version)
    width = max(len(target), len(candidate))
    if target + (0,) * (width - len(target)) >= candidate + (0,) * (
        width - len(candidate)
    ):
        raise ValueError("New version must be greater than the target version.")


def clean_text(value: str, *, field: str, max_length: int) -> str:
    cleaned = " ".join(value.split()).strip()
    if not cleaned:
        raise ValueError(f"{field} is required.")
    if len(cleaned) > max_length:
        raise ValueError(f"{field} must not exceed {max_length} characters.")
    return cleaned


def safe_filename(value: str) -> str:
    cleaned = " ".join(value.replace("\x00", "").split()).strip()
    if not cleaned:
        return "uploaded file"
    return cleaned[:120]


def knowledge_file_display_title(filename: str) -> str:
    """Remove only a final RAG-supported extension for visible copy."""
    stem, separator, extension = filename.rpartition(".")
    if separator and stem and f".{extension.lower()}" in _KNOWLEDGE_FILE_EXTENSIONS:
        return stem
    return filename


@dataclass(frozen=True)
class DeliveryResult:
    message_id: str
    topic: str


class ConfirmedPreSendNotificationError(RuntimeError):
    """The notification was rejected before an FCM dispatch was attempted.

    Only this explicit error is safe for an automatic retry. Exceptions raised
    by ``messaging.send`` are intentionally left unwrapped because a transport
    failure can occur after FCM accepted the request.
    """


class FcmSender:
    @staticmethod
    def _message_config() -> dict[str, Any]:
        return {
            "android": messaging.AndroidConfig(priority="high"),
            "apns": messaging.APNSConfig(
                headers={"apns-priority": "10"},
                payload=messaging.APNSPayload(
                    aps=messaging.Aps(sound="default")
                ),
            ),
        }

    async def send_general_audience(
        self, *, title: str, body: str, data: dict[str, str]
    ) -> DeliveryResult:
        """Send one FCM message to new clients and legacy App Store iOS users."""
        if local_notification_test_enabled():
            raise ConfirmedPreSendNotificationError(
                "Condition delivery is forbidden in local notification test mode."
            )
        try:
            message = messaging.Message(
                condition=GENERAL_AUDIENCE_CONDITION,
                notification=messaging.Notification(title=title, body=body),
                data={key: str(value) for key, value in data.items()},
                **self._message_config(),
            )
        except (TypeError, ValueError) as exc:
            raise ConfirmedPreSendNotificationError(str(exc)) from exc
        message_id = await asyncio.to_thread(messaging.send, message)
        # Keep the existing logical-audience metadata/API response stable. The
        # physical FCM destination is GENERAL_AUDIENCE_CONDITION above.
        return DeliveryResult(message_id=message_id, topic=GENERAL_TOPIC)

    async def send_topic(
        self, *, topic: str, title: str, body: str, data: dict[str, str]
    ) -> DeliveryResult:
        if local_notification_test_enabled():
            raise ConfirmedPreSendNotificationError(
                "Topic delivery is forbidden in local notification test mode."
            )
        try:
            message = messaging.Message(
                topic=topic,
                notification=messaging.Notification(title=title, body=body),
                data={key: str(value) for key, value in data.items()},
                **self._message_config(),
            )
        except (TypeError, ValueError) as exc:
            raise ConfirmedPreSendNotificationError(str(exc)) from exc
        message_id = await asyncio.to_thread(messaging.send, message)
        return DeliveryResult(message_id=message_id, topic=topic)


class LocalSingleDeviceSender:
    """Test-only sender that cannot express an FCM topic destination."""

    async def send_topic(
        self, *, topic: str, title: str, body: str, data: dict[str, str]
    ) -> DeliveryResult:
        # ``topic`` is accepted only to preserve the production service's
        # payload-building path. It is never forwarded to Firebase or stored as
        # a destination.
        if topic not in {GENERAL_TOPIC} and not topic.startswith("version_"):
            raise ConfirmedPreSendNotificationError("Unexpected logical audience.")
        try:
            message_id, destination = await asyncio.to_thread(
                deliver_local_test_message,
                title=title,
                body=body,
                data=data,
            )
        except (TypeError, ValueError) as exc:
            raise ConfirmedPreSendNotificationError(str(exc)) from exc
        return DeliveryResult(message_id=message_id, topic=destination)

    async def send_general_audience(
        self, *, title: str, body: str, data: dict[str, str]
    ) -> DeliveryResult:
        # Local safety mode always resolves logical audiences to its one
        # explicitly configured device; it never forwards an FCM condition.
        return await self.send_topic(
            topic=GENERAL_TOPIC, title=title, body=body, data=data
        )


def default_sender() -> FcmSender | LocalSingleDeviceSender:
    return LocalSingleDeviceSender() if local_notification_test_enabled() else FcmSender()


class NotificationRepository:
    def __init__(self, db: Any | None = None) -> None:
        self.db = db or firestore_async.client()

    async def get_or_create_announcement(
        self,
        title: str,
        body: str,
        admin_uid: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        fingerprint = hashlib.sha256(f"{title}\0{body}".encode()).hexdigest()
        announcement_id = hashlib.sha256(
            f"announcement\0{admin_uid}\0{idempotency_key}".encode()
        ).hexdigest()[:40]
        ref = self.db.collection(ANNOUNCEMENTS_COLLECTION).document(announcement_id)
        transaction = self.db.transaction()

        @firestore_async.async_transactional
        async def create_or_read(transaction: Any) -> dict[str, Any]:
            snapshot = await ref.get(transaction=transaction)
            if snapshot.exists:
                data = snapshot.to_dict() or {}
                if (
                    data.get("requestFingerprint") != fingerprint
                    or data.get("createdBy") != admin_uid
                ):
                    raise AnnouncementIdempotencyConflict(
                        "Idempotency-Key was already used for different content."
                    )
                return {
                    "id": announcement_id,
                    "created": False,
                    "deliveryStatus": (data.get("delivery") or {}).get(
                        "status", "pending"
                    ),
                }
            transaction.set(
                ref,
                {
                    "title": title,
                    "body": body,
                    "type": "announcement",
                    "createdAt": firestore.SERVER_TIMESTAMP,
                    "createdBy": admin_uid,
                    "idempotencyKey": idempotency_key,
                    "requestFingerprint": fingerprint,
                    "delivery": {"status": "pending", "topic": GENERAL_TOPIC},
                    "viewCount": 0,
                },
            )
            return {
                "id": announcement_id,
                "created": True,
                "deliveryStatus": "pending",
            }

        return await create_or_read(transaction)

    async def claim_announcement_delivery(self, announcement_id: str) -> str:
        ref = self.db.collection(ANNOUNCEMENTS_COLLECTION).document(announcement_id)
        transaction = self.db.transaction()
        now = datetime.now(timezone.utc)

        @firestore_async.async_transactional
        async def claim(transaction: Any) -> str:
            snapshot = await ref.get(transaction=transaction)
            if not snapshot.exists:
                raise AnnouncementStorageError("Announcement disappeared before delivery.")
            delivery = (snapshot.to_dict() or {}).get("delivery") or {}
            current = str(delivery.get("status") or "pending")
            if current in {"sent", "delivery_unknown"}:
                return current
            if current == "dispatching":
                started_at = delivery.get("startedAt")
                if isinstance(started_at, datetime):
                    if started_at.tzinfo is None:
                        started_at = started_at.replace(tzinfo=timezone.utc)
                    if started_at > now - _DISPATCH_STALE_AFTER:
                        return "pending"
                transaction.update(
                    ref,
                    {
                        "delivery.status": "delivery_unknown",
                        "delivery.disposition": "interrupted_dispatch",
                        "delivery.updatedAt": firestore.SERVER_TIMESTAMP,
                    },
                )
                return "delivery_unknown"
            if current not in {"pending", "confirmed_pre_dispatch_failure"}:
                return "delivery_unknown"
            transaction.update(
                ref,
                {
                    "delivery.status": "dispatching",
                    "delivery.startedAt": now,
                    "delivery.attempts": int(delivery.get("attempts") or 0) + 1,
                    "delivery.updatedAt": firestore.SERVER_TIMESTAMP,
                },
            )
            return "dispatching"

        return await claim(transaction)

    async def mark_delivery(
        self,
        announcement_id: str,
        *,
        status: str,
        message_id: str | None = None,
        error: str | None = None,
        disposition: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "delivery.status": status,
            "delivery.updatedAt": firestore.SERVER_TIMESTAMP,
        }
        if message_id:
            payload["delivery.messageId"] = message_id
        if error:
            payload["delivery.error"] = error[:500]
        if disposition:
            payload["delivery.disposition"] = disposition
        await self.db.collection(ANNOUNCEMENTS_COLLECTION).document(
            announcement_id
        ).update(payload)

    async def latest_announcements(self, limit: int = 3) -> list[dict[str, Any]]:
        query = (
            self.db.collection(ANNOUNCEMENTS_COLLECTION)
            .order_by("createdAt", direction=firestore.Query.DESCENDING)
            .limit(limit)
        )
        return [self._public_announcement(snapshot) async for snapshot in query.stream()]

    async def announcement_stats(self) -> list[dict[str, Any]]:
        query = self.db.collection(ANNOUNCEMENTS_COLLECTION).order_by(
            "createdAt", direction=firestore.Query.DESCENDING
        )
        results: list[dict[str, Any]] = []
        async for snapshot in query.stream():
            data = snapshot.to_dict() or {}
            if data.get("type") != "announcement":
                continue
            results.append(
                {
                    **self._public_announcement(snapshot),
                    "viewCount": int(data.get("viewCount") or 0),
                    "deliveryStatus": (data.get("delivery") or {}).get("status"),
                }
            )
        return results

    async def record_view(
        self,
        announcement_id: str,
        viewer_hash: str,
        *,
        authenticated: bool,
    ) -> bool:
        announcement_ref = self.db.collection(ANNOUNCEMENTS_COLLECTION).document(
            announcement_id
        )
        view_ref = announcement_ref.collection("views").document(viewer_hash)
        transaction = self.db.transaction()

        @firestore_async.async_transactional
        async def update_in_transaction(transaction: Any) -> bool:
            announcement = await announcement_ref.get(transaction=transaction)
            if not announcement.exists or (announcement.to_dict() or {}).get("type") != "announcement":
                raise LookupError("Announcement not found.")
            existing = await view_ref.get(transaction=transaction)
            if existing.exists:
                if authenticated and not (existing.to_dict() or {}).get("authenticatedSeen"):
                    transaction.update(view_ref, {"authenticatedSeen": True})
                return False
            transaction.set(
                view_ref,
                {
                    "firstViewedAt": firestore.SERVER_TIMESTAMP,
                    "authenticatedSeen": authenticated,
                },
            )
            transaction.update(announcement_ref, {"viewCount": firestore.Increment(1)})
            return True

        return await update_in_transaction(transaction)

    @staticmethod
    def _public_announcement(snapshot: Any) -> dict[str, Any]:
        data = snapshot.to_dict() or {}
        created_at = data.get("createdAt")
        return {
            "id": snapshot.id,
            "type": "announcement",
            "title": str(data.get("title") or ""),
            "body": str(data.get("body") or ""),
            "createdAt": created_at.isoformat() if hasattr(created_at, "isoformat") else None,
        }


class NotificationService:
    def __init__(
        self,
        repository: NotificationRepository | None = None,
        sender: FcmSender | None = None,
    ) -> None:
        self.repository = repository or NotificationRepository()
        self.sender = sender or default_sender()

    async def _send_general_audience(
        self, *, title: str, body: str, data: dict[str, str]
    ) -> DeliveryResult:
        """Use condition delivery without breaking legacy injected senders.

        Older test doubles and integrations may implement only ``send_topic``.
        The fallback preserves that interface while the production sender uses
        exactly one condition-addressed Firebase message.
        """
        send_general = getattr(self.sender, "send_general_audience", None)
        if send_general is not None:
            return await send_general(title=title, body=body, data=data)
        return await self.sender.send_topic(
            topic=GENERAL_TOPIC, title=title, body=body, data=data
        )

    async def create_announcement(
        self,
        title: str,
        body: str,
        admin_uid: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        title = clean_text(title, field="title", max_length=120)
        body = clean_text(body, field="body", max_length=1000)
        key = (idempotency_key or uuid.uuid4().hex).strip()
        if not _IDEMPOTENCY_RE.fullmatch(key):
            raise ValueError(
                "Idempotency-Key must be 16 to 128 URL-safe characters."
            )
        try:
            record = await self.repository.get_or_create_announcement(
                title, body, admin_uid, key
            )
        except AnnouncementIdempotencyConflict:
            raise
        except Exception as exc:
            raise AnnouncementStorageError(
                "Announcement creation could not be confirmed; no push was attempted.",
                idempotency_key=key,
            ) from exc
        announcement_id = str(record["id"])
        existing_status = str(record.get("deliveryStatus") or "pending")
        if existing_status == "sent":
            return {
                "id": announcement_id,
                "idempotencyKey": key,
                "created": False,
                "deliveryStatus": "sent",
                "topic": GENERAL_TOPIC,
            }
        if existing_status == "delivery_unknown":
            raise AnnouncementDeliveryError(
                announcement_id=announcement_id,
                idempotency_key=key,
                delivery_status="delivery_unknown",
                message="Announcement exists, but the previous push outcome is unknown.",
            )
        try:
            claim = await self.repository.claim_announcement_delivery(announcement_id)
        except Exception as exc:
            raise AnnouncementDeliveryError(
                announcement_id=announcement_id,
                idempotency_key=key,
                delivery_status="delivery_unknown",
                message="Announcement was saved, but delivery could not be claimed safely.",
            ) from exc
        if claim == "sent":
            return {
                "id": announcement_id,
                "idempotencyKey": key,
                "created": False,
                "deliveryStatus": "sent",
                "topic": GENERAL_TOPIC,
            }
        if claim == "pending":
            return {
                "id": announcement_id,
                "idempotencyKey": key,
                "created": bool(record.get("created")),
                "deliveryStatus": "pending",
                "topic": GENERAL_TOPIC,
            }
        if claim != "dispatching":
            raise AnnouncementDeliveryError(
                announcement_id=announcement_id,
                idempotency_key=key,
                delivery_status="delivery_unknown",
                message="Announcement exists, but push delivery may already have occurred.",
            )
        payload = {
            "type": "announcement",
            "event_id": announcement_id,
            "announcement_id": announcement_id,
            "title": title,
            "body": body,
        }
        try:
            result = await self._send_general_audience(
                title=title, body=body, data=payload
            )
        except ConfirmedPreSendNotificationError as exc:
            try:
                await self.repository.mark_delivery(
                    announcement_id,
                    status="confirmed_pre_dispatch_failure",
                    error=str(exc),
                    disposition="confirmed_pre_dispatch_failure",
                )
            except Exception as storage_exc:
                raise AnnouncementDeliveryError(
                    announcement_id=announcement_id,
                    idempotency_key=key,
                    delivery_status="delivery_unknown",
                    message="Push was rejected before dispatch, but its state could not be saved.",
                ) from storage_exc
            raise AnnouncementDeliveryError(
                announcement_id=announcement_id,
                idempotency_key=key,
                delivery_status="confirmed_pre_dispatch_failure",
                message="Announcement was saved, but the push was not dispatched.",
            ) from exc
        except Exception as exc:
            try:
                await self.repository.mark_delivery(
                    announcement_id,
                    status="delivery_unknown",
                    error=str(exc),
                    disposition="ambiguous_fcm_result",
                )
            except Exception:
                logger.exception(
                    "Could not persist ambiguous announcement delivery for %s",
                    announcement_id,
                )
            raise AnnouncementDeliveryError(
                announcement_id=announcement_id,
                idempotency_key=key,
                delivery_status="delivery_unknown",
                message="Announcement was saved, but FCM delivery is unknown.",
            ) from exc
        try:
            await self.repository.mark_delivery(
                announcement_id,
                status="sent",
                message_id=result.message_id,
                disposition="accepted",
            )
        except Exception as exc:
            try:
                await self.repository.mark_delivery(
                    announcement_id,
                    status="delivery_unknown",
                    error=str(exc),
                    disposition="accepted_but_not_persisted",
                )
            except Exception:
                logger.exception(
                    "Could not persist post-send ambiguity for announcement %s",
                    announcement_id,
                )
            raise AnnouncementDeliveryError(
                announcement_id=announcement_id,
                idempotency_key=key,
                delivery_status="delivery_unknown",
                message="FCM accepted the request, but delivery state could not be saved.",
            ) from exc
        return {
            "id": announcement_id,
            "idempotencyKey": key,
            "created": bool(record.get("created")),
            "deliveryStatus": "sent",
            "topic": result.topic,
        }

    async def send_app_update(
        self,
        *,
        title: str,
        body: str,
        target_version: str,
        new_version: str,
        force: bool,
    ) -> dict[str, Any]:
        title = clean_text(title, field="title", max_length=120)
        body = clean_text(body, field="body", max_length=1000)
        target_version, _ = parse_version(target_version)
        new_version, _ = parse_version(new_version)
        topic = version_topic(target_version)
        validate_version_upgrade(target_version, new_version)
        event_id = hashlib.sha256(
            f"{target_version}|{new_version}|{force}|{title}|{body}".encode()
        ).hexdigest()[:32]
        result = await self.sender.send_topic(
            topic=topic,
            title=title,
            body=body,
            data={
                "type": "app_update",
                "event_id": event_id,
                "version": new_version,
                "target_version": target_version,
                "force": "true" if force else "false",
                "title": title,
                "body": body,
            },
        )
        return {"eventId": event_id, "deliveryStatus": "sent", "topic": result.topic}

    async def send_knowledge_file(self, *, file_id: str, filename: str) -> DeliveryResult:
        filename = safe_filename(filename)
        display_title = knowledge_file_display_title(filename)
        title = f"ملف معرفي جديد: {display_title}"
        body = f"أصبح ملف {display_title} متاحًا للبحث. افتحه لكتابة سؤال عنه."
        return await self._send_general_audience(
            title=title[:120],
            body=body[:1000],
            data={
                "type": "knowledge_file",
                "event_id": f"knowledge:{file_id}",
                "file_id": file_id,
                "filename": filename,
                "title": title[:120],
                "body": body[:1000],
            },
        )


def viewer_identity(*, installation_id: str, uid: str | None) -> str:
    if not _INSTALLATION_RE.fullmatch(installation_id):
        raise ValueError("installationId is invalid.")
    pepper = os.getenv("ANALYTICS_ID_PEPPER", "")
    if len(pepper) < 32:
        raise RuntimeError("ANALYTICS_ID_PEPPER must be configured with at least 32 characters.")
    # Installation identity is canonical so an anonymous view and a later signed-in
    # view on the same installation reconcile to one count. It is an abuse-resistant
    # estimate, not proof of a unique human. UID presence is recorded only as a bool.
    return hmac.new(
        pepper.encode(), f"installation:{installation_id}".encode(), hashlib.sha256
    ).hexdigest()
