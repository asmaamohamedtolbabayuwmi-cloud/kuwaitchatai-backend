"""Durable, leased post-indexing notification jobs.

FCM topic delivery is not exactly once. A worker transactionally changes a job
to ``sending`` immediately before dispatch. A confirmed local/pre-send failure
may become ``notification_failed`` and is safe to retry. Every other send
exception, or a crash/write failure after entering ``sending``, is treated as
``delivery_unknown`` and is never retried automatically because FCM may already
have accepted the message.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from firebase_admin import firestore, firestore_async
from google.api_core.exceptions import AlreadyExists

from ..config import get_config
from ..openai_client import get_openai_client
from ..task_tracker import background_tasks
from .notification_service import (
    KNOWLEDGE_JOBS_COLLECTION,
    ConfirmedPreSendNotificationError,
    NotificationService,
    safe_filename,
)

logger = logging.getLogger(__name__)
TERMINAL_FAILURES = {"failed", "cancelled"}
RUNNABLE_STATES = {"indexing", "notification_failed"}
HOLD_STATES = {"sending", "delivery_unknown"}
TERMINAL_STATES = {
    "sent",
    "indexing_failed",
    "source_missing",
    "source_error",
    "timed_out",
}
RECOVERY_STATES = sorted(RUNNABLE_STATES | HOLD_STATES)
DEFAULT_LEASE_SECONDS = 300
OPERATOR_NOTE = (
    "FCM delivery may have succeeded. Do not retry automatically. Review Railway "
    "and Firebase logs; only reset the job if the duplicate-delivery risk is accepted."
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _lease_is_active(expires_at: Any, now: datetime) -> bool:
    if not isinstance(expires_at, datetime):
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at > now


def _openai_error_kind(exc: Exception) -> str:
    """Classify a vector-store lookup failure without depending on SDK internals."""
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in {404, 410}:
        return "missing"
    if status in {408, 409, 425, 429} or (isinstance(status, int) and status >= 500):
        return "transient"
    if status in {400, 401, 403, 422}:
        return "permanent"
    name = type(exc).__name__.lower()
    if any(part in name for part in ("timeout", "connection", "ratelimit", "tempor")):
        return "transient"
    return "permanent"


async def _acquire_lease(
    ref: Any,
    db: Any,
    *,
    owner: str,
    clock: Callable[[], datetime],
    lease_seconds: int,
) -> str:
    transaction = db.transaction()

    @firestore_async.async_transactional
    async def acquire(transaction: Any) -> str:
        snapshot = await ref.get(transaction=transaction)
        if not snapshot.exists:
            return "missing"
        data = snapshot.to_dict() or {}
        state = str(data.get("state") or "indexing")
        if state in TERMINAL_STATES | HOLD_STATES:
            return state
        attempt_now = clock()
        current_owner = data.get("leaseOwner")
        if (
            current_owner
            and current_owner != owner
            and _lease_is_active(data.get("leaseExpiresAt"), attempt_now)
        ):
            return "leased"
        transaction.update(
            ref,
            {
                "leaseOwner": owner,
                "leaseExpiresAt": attempt_now + timedelta(seconds=lease_seconds),
                "updatedAt": firestore.SERVER_TIMESTAMP,
            },
        )
        return "acquired"

    return await acquire(transaction)


async def _renew_lease(
    ref: Any,
    db: Any,
    *,
    owner: str,
    clock: Callable[[], datetime],
    lease_seconds: int,
) -> bool:
    transaction = db.transaction()

    @firestore_async.async_transactional
    async def renew(transaction: Any) -> bool:
        snapshot = await ref.get(transaction=transaction)
        data = snapshot.to_dict() or {}
        if not snapshot.exists or data.get("leaseOwner") != owner:
            return False
        if data.get("state") not in RUNNABLE_STATES:
            return False
        attempt_now = clock()
        if not _lease_is_active(data.get("leaseExpiresAt"), attempt_now):
            return False
        transaction.update(
            ref,
            {
                "leaseExpiresAt": attempt_now + timedelta(seconds=lease_seconds),
                "updatedAt": firestore.SERVER_TIMESTAMP,
            },
        )
        return True

    return await renew(transaction)


def _released_state(state: str, **values: Any) -> dict[str, Any]:
    return {
        "state": state,
        "leaseOwner": None,
        "leaseExpiresAt": None,
        "updatedAt": firestore.SERVER_TIMESTAMP,
        **values,
    }


async def _update_owned_job(
    ref: Any,
    db: Any,
    *,
    owner: str,
    clock: Callable[[], datetime],
    allowed_states: set[str],
    values: dict[str, Any],
) -> str:
    """Update only while this worker still owns an active lease."""
    transaction = db.transaction()

    @firestore_async.async_transactional
    async def update(transaction: Any) -> str:
        snapshot = await ref.get(transaction=transaction)
        if not snapshot.exists:
            return "missing"
        data = snapshot.to_dict() or {}
        state = str(data.get("state") or "indexing")
        if state not in allowed_states:
            return state
        attempt_now = clock()
        if data.get("leaseOwner") != owner or not _lease_is_active(
            data.get("leaseExpiresAt"), attempt_now
        ):
            return "leased"
        transaction.update(ref, values)
        return "updated"

    return await update(transaction)


async def _quarantine_expired_sending(
    ref: Any,
    db: Any,
    *,
    clock: Callable[[], datetime],
) -> str:
    """Atomically quarantine only a currently-sending job with an expired lease."""
    transaction = db.transaction()

    @firestore_async.async_transactional
    async def quarantine(transaction: Any) -> str:
        snapshot = await ref.get(transaction=transaction)
        if not snapshot.exists:
            return "missing"
        data = snapshot.to_dict() or {}
        state = str(data.get("state") or "indexing")
        if state != "sending":
            return state
        if _lease_is_active(data.get("leaseExpiresAt"), clock()):
            return "leased"
        transaction.update(
            ref,
            _released_state(
                "delivery_unknown",
                deliveryDisposition="worker_interrupted_during_send",
                operatorNote=OPERATOR_NOTE,
            ),
        )
        return "delivery_unknown"

    return await quarantine(transaction)


async def create_knowledge_notification_job(file_id: str, filename: str) -> None:
    db = firestore_async.client()
    ref = db.collection(KNOWLEDGE_JOBS_COLLECTION).document(file_id)
    try:
        await ref.create(
            {
                "fileId": file_id,
                "filename": safe_filename(filename),
                "state": "indexing",
                "attempts": 0,
                "createdAt": firestore.SERVER_TIMESTAMP,
                "updatedAt": firestore.SERVER_TIMESTAMP,
            }
        )
    except AlreadyExists:
        snapshot = await ref.get()
        data = snapshot.to_dict() or {}
        if not snapshot.exists or data.get("fileId") != file_id:
            raise


async def process_knowledge_notification_job(
    file_id: str,
    *,
    max_attempts: int = 60,
    poll_seconds: float = 5.0,
    delay=asyncio.sleep,
    db: Any | None = None,
    client: Any | None = None,
    vector_store_id: str | None = None,
    notification_service: NotificationService | None = None,
    owner_id: str | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    clock: Callable[[], datetime] = _utcnow,
) -> str:
    try:
        db = db or firestore_async.client()
        ref = db.collection(KNOWLEDGE_JOBS_COLLECTION).document(file_id)
        client = client or get_openai_client()
        vector_store_id = vector_store_id or get_config().OPENAI_VECTOR_STORE_ID
        notification_service = notification_service or NotificationService()
        owner = owner_id or f"worker-{uuid.uuid4().hex}"

        lease_result = await _acquire_lease(
            ref,
            db,
            owner=owner,
            clock=clock,
            lease_seconds=lease_seconds,
        )
        if lease_result != "acquired":
            return lease_result

        while True:
            snapshot = await ref.get()
            if not snapshot.exists:
                return "missing"
            job = snapshot.to_dict() or {}
            if job.get("leaseOwner") != owner:
                return "leased"
            state = str(job.get("state") or "indexing")
            if state not in RUNNABLE_STATES:
                return state
            retrying_confirmed_failure = (
                state == "notification_failed"
                and job.get("deliveryDisposition") == "confirmed_pre_send_failure"
                and job.get("indexingStatus") == "completed"
            )
            attempt = int(job.get("attempts") or 0)
            if not retrying_confirmed_failure:
                attempt += 1
            if not retrying_confirmed_failure and attempt > max_attempts:
                update_result = await _update_owned_job(
                    ref,
                    db,
                    owner=owner,
                    clock=clock,
                    allowed_states=RUNNABLE_STATES,
                    values=_released_state("timed_out"),
                )
                return "timed_out" if update_result == "updated" else update_result
            if not await _renew_lease(
                ref,
                db,
                owner=owner,
                clock=clock,
                lease_seconds=lease_seconds,
            ):
                return "leased"

            try:
                if retrying_confirmed_failure:
                    indexing_status = "completed"
                else:
                    vs_file = await client.vector_stores.files.retrieve(
                        file_id=file_id, vector_store_id=vector_store_id
                    )
                    indexing_status = str(vs_file.status)
            except Exception as exc:
                kind = _openai_error_kind(exc)
                error = str(exc)[:500]
                if kind == "missing":
                    logger.error("Knowledge source %s is missing or deleted: %s", file_id, exc)
                    update_result = await _update_owned_job(
                        ref,
                        db,
                        owner=owner,
                        clock=clock,
                        allowed_states=RUNNABLE_STATES,
                        values=_released_state(
                            "source_missing",
                            attempts=attempt,
                            indexingStatus="missing",
                            error=error,
                        ),
                    )
                    return "source_missing" if update_result == "updated" else update_result
                if kind == "permanent":
                    logger.error("Permanent index lookup failure for %s: %s", file_id, exc)
                    update_result = await _update_owned_job(
                        ref,
                        db,
                        owner=owner,
                        clock=clock,
                        allowed_states=RUNNABLE_STATES,
                        values=_released_state(
                            "source_error",
                            attempts=attempt,
                            indexingStatus="lookup_failed",
                            error=error,
                        ),
                    )
                    return "source_error" if update_result == "updated" else update_result
                logger.warning("Transient index lookup failure for %s: %s", file_id, exc)
                if attempt >= max_attempts:
                    update_result = await _update_owned_job(
                        ref,
                        db,
                        owner=owner,
                        clock=clock,
                        allowed_states=RUNNABLE_STATES,
                        values=_released_state(
                            "timed_out",
                            attempts=attempt,
                            indexingStatus="lookup_retry_exhausted",
                            error=error,
                        ),
                    )
                    return "timed_out" if update_result == "updated" else update_result
                update_result = await _update_owned_job(
                    ref,
                    db,
                    owner=owner,
                    clock=clock,
                    allowed_states=RUNNABLE_STATES,
                    values={
                        "attempts": attempt,
                        "indexingStatus": "lookup_retry",
                        "error": error,
                        "updatedAt": firestore.SERVER_TIMESTAMP,
                    },
                )
                if update_result != "updated":
                    return update_result
                await delay(poll_seconds)
                continue

            update_result = await _update_owned_job(
                ref,
                db,
                owner=owner,
                clock=clock,
                allowed_states=RUNNABLE_STATES,
                values={
                    "attempts": attempt,
                    "indexingStatus": indexing_status,
                    "error": None,
                    "updatedAt": firestore.SERVER_TIMESTAMP,
                },
            )
            if update_result != "updated":
                return update_result
            if indexing_status == "completed":
                transaction = db.transaction()

                @firestore_async.async_transactional
                async def claim_send(transaction: Any) -> str:
                    current = await ref.get(transaction=transaction)
                    data = current.to_dict() or {}
                    current_state = str(data.get("state") or "indexing")
                    if not current.exists:
                        return "missing"
                    if current_state not in RUNNABLE_STATES:
                        return current_state
                    if data.get("leaseOwner") != owner:
                        return "leased"
                    claim_now = clock()
                    if not _lease_is_active(data.get("leaseExpiresAt"), claim_now):
                        return "leased"
                    transaction.update(
                        ref,
                        {
                            "state": "sending",
                            "deliveryAttempts": int(data.get("deliveryAttempts") or 0) + 1,
                            "sendStartedAt": firestore.SERVER_TIMESTAMP,
                            "leaseExpiresAt": claim_now + timedelta(seconds=lease_seconds),
                            "updatedAt": firestore.SERVER_TIMESTAMP,
                        },
                    )
                    return "sending"

                claim_result = await claim_send(transaction)
                if claim_result != "sending":
                    return claim_result
                try:
                    result = await notification_service.send_knowledge_file(
                        file_id=file_id,
                        filename=str(job.get("filename") or file_id),
                    )
                except ConfirmedPreSendNotificationError as exc:
                    logger.warning("Confirmed pre-send failure for %s: %s", file_id, exc)
                    update_result = await _update_owned_job(
                        ref,
                        db,
                        owner=owner,
                        clock=clock,
                        allowed_states={"sending"},
                        values=_released_state(
                            "notification_failed",
                            error=str(exc)[:500],
                            deliveryDisposition="confirmed_pre_send_failure",
                        ),
                    )
                    return (
                        "notification_failed"
                        if update_result == "updated"
                        else update_result
                    )
                except Exception as exc:
                    logger.exception(
                        "Ambiguous FCM result for %s; automatic retry is disabled",
                        file_id,
                    )
                    await _update_owned_job(
                        ref,
                        db,
                        owner=owner,
                        clock=clock,
                        allowed_states={"sending"},
                        values=_released_state(
                            "delivery_unknown",
                            error=str(exc)[:500],
                            deliveryDisposition="unknown",
                            operatorNote=OPERATOR_NOTE,
                        ),
                    )
                    return "delivery_unknown"
                try:
                    update_result = await _update_owned_job(
                        ref,
                        db,
                        owner=owner,
                        clock=clock,
                        allowed_states={"sending"},
                        values=_released_state(
                            "sent",
                            messageId=result.message_id,
                            sentAt=firestore.SERVER_TIMESTAMP,
                            deliveryDisposition="accepted",
                        ),
                    )
                    if update_result != "updated":
                        logger.error(
                            "FCM accepted %s after lease ownership was lost; stale sending recovery will quarantine it",
                            file_id,
                        )
                        return "sent" if update_result == "sent" else "delivery_unknown"
                except Exception as exc:
                    logger.exception(
                        "FCM accepted %s but persisting the result failed; delivery is unknown",
                        file_id,
                    )
                    try:
                        await _update_owned_job(
                            ref,
                            db,
                            owner=owner,
                            clock=clock,
                            allowed_states={"sending"},
                            values=_released_state(
                                "delivery_unknown",
                                error=str(exc)[:500],
                                deliveryDisposition="accepted_but_not_persisted",
                                operatorNote=OPERATOR_NOTE,
                            ),
                        )
                    except Exception:
                        logger.exception(
                            "Could not persist delivery_unknown for %s; stale sending recovery will quarantine it",
                            file_id,
                        )
                    return "delivery_unknown"
                return "sent"
            if indexing_status in TERMINAL_FAILURES:
                update_result = await _update_owned_job(
                    ref,
                    db,
                    owner=owner,
                    clock=clock,
                    allowed_states=RUNNABLE_STATES,
                    values=_released_state(
                        "indexing_failed", error="OpenAI indexing failed"
                    ),
                )
                return "indexing_failed" if update_result == "updated" else update_result
            if indexing_status != "in_progress":
                logger.error("Unexpected OpenAI indexing status %r for %s", indexing_status, file_id)
                update_result = await _update_owned_job(
                    ref,
                    db,
                    owner=owner,
                    clock=clock,
                    allowed_states=RUNNABLE_STATES,
                    values=_released_state(
                        "source_error",
                        error=f"Unexpected indexing status: {indexing_status}"[:500],
                    ),
                )
                return "source_error" if update_result == "updated" else update_result
            if attempt >= max_attempts:
                update_result = await _update_owned_job(
                    ref,
                    db,
                    owner=owner,
                    clock=clock,
                    allowed_states=RUNNABLE_STATES,
                    values=_released_state("timed_out"),
                )
                return "timed_out" if update_result == "updated" else update_result
            await delay(poll_seconds)
    except Exception:
        logger.exception(
            "Knowledge notification worker failed for %s; chat and RAG remain available",
            file_id,
        )
        return "worker_error"


async def _reconcile_sending_after_lease(
    file_id: str,
    *,
    wait_seconds: float,
    db: Any,
    delay: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    clock: Callable[[], datetime] = _utcnow,
) -> None:
    """Quarantine an interrupted send after its active lease has had time to finish."""
    try:
        await delay(max(0.0, wait_seconds))
        ref = db.collection(KNOWLEDGE_JOBS_COLLECTION).document(file_id)
        result = await _quarantine_expired_sending(ref, db, clock=clock)
        if result == "delivery_unknown":
            logger.error("Quarantined expired sending job %s for operator review", file_id)
    except Exception:
        logger.exception("Deferred sending-state reconciliation failed for %s", file_id)


async def resume_pending_knowledge_notification_jobs(
    *,
    db: Any | None = None,
    task_tracker: Any = background_tasks,
    clock: Callable[[], datetime] = _utcnow,
) -> int:
    """Schedule retry-safe jobs and quarantine stale ambiguous sends.

    ``delivery_unknown`` is only logged for operator review. An expired
    ``sending`` lease is changed to ``delivery_unknown`` and is never sent by
    recovery. Leases ensure that concurrent replicas can scan safely.
    """
    db = db or firestore_async.client()
    query = db.collection(KNOWLEDGE_JOBS_COLLECTION).where(
        "state", "in", RECOVERY_STATES
    )
    count = 0
    failures = 0
    async for snapshot in query.stream():
        data = snapshot.to_dict() or {}
        state = str(data.get("state") or "indexing")
        if state == "delivery_unknown":
            logger.error("Knowledge job %s requires operator review: %s", snapshot.id, OPERATOR_NOTE)
            continue
        if state == "sending":
            now = clock()
            if _lease_is_active(data.get("leaseExpiresAt"), now):
                logger.info("Knowledge job %s still has an active send lease", snapshot.id)
                expires_at = data["leaseExpiresAt"]
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                coroutine = _reconcile_sending_after_lease(
                    snapshot.id,
                    wait_seconds=(expires_at - now).total_seconds() + 1,
                    db=db,
                    clock=clock,
                )
                try:
                    task_tracker.create_task(coroutine)
                except Exception:
                    coroutine.close()
                    failures += 1
                    logger.exception(
                        "Could not schedule deferred reconciliation for %s", snapshot.id
                    )
                continue
            try:
                result = await _quarantine_expired_sending(
                    snapshot.reference,
                    db,
                    clock=clock,
                )
            except Exception:
                failures += 1
                logger.exception("Could not quarantine stale sending job %s", snapshot.id)
            else:
                if result != "delivery_unknown":
                    logger.info(
                        "Skipped stale-send quarantine for %s because its state or lease changed",
                        snapshot.id,
                    )
                    continue
                logger.error("Quarantined stale sending job %s for operator review", snapshot.id)
            continue
        coroutine = process_knowledge_notification_job(snapshot.id)
        try:
            task_tracker.create_task(coroutine)
        except Exception:
            coroutine.close()
            failures += 1
            logger.exception("Could not schedule recovered knowledge job %s", snapshot.id)
            continue
        count += 1
    if failures:
        raise RuntimeError(f"{failures} knowledge-job recovery operation(s) failed")
    return count


async def recover_pending_knowledge_notification_jobs(
    *,
    max_attempts: int = 3,
    base_delay_seconds: float = 5.0,
    max_delay_seconds: float = 60.0,
    delay: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    resume: Callable[[], Awaitable[int]] = resume_pending_knowledge_notification_jobs,
) -> int:
    """Best-effort startup recovery with bounded exponential retry.

    This function never raises. Exhausted recovery affects notification jobs
    only and must not make the FastAPI process unavailable.
    """
    attempts = max(1, max_attempts)
    for attempt in range(1, attempts + 1):
        try:
            resumed = await resume()
            if resumed:
                logger.info("Resumed %d knowledge-file notification jobs", resumed)
            return resumed
        except Exception:
            logger.exception(
                "Knowledge-job startup recovery attempt %d/%d failed",
                attempt,
                attempts,
            )
            if attempt < attempts:
                wait = min(base_delay_seconds * (2 ** (attempt - 1)), max_delay_seconds)
                await delay(wait)
    logger.error(
        "Knowledge-job recovery exhausted after %d attempts; core API startup continues",
        attempts,
    )
    return 0
