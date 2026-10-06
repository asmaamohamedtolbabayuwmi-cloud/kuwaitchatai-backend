"""Firestore persistence for immutable schedule versions."""

from __future__ import annotations

from firebase_admin import firestore_async
from google.api_core.exceptions import AlreadyExists
from google.cloud import firestore

from .services.schedule_fingerprint import ScheduleIdentity
from .services.schedule_models import ScheduleExtraction


async def create_schedule_version(
    uid: str,
    extraction: ScheduleExtraction,
    identity: ScheduleIdentity,
    source_filename: str,
) -> bool:
    """Create a schedule once. Return False when its fingerprint already exists."""
    db = firestore_async.client()
    schedule_ref = (
        db.collection("users")
        .document(uid)
        .collection("schedules")
        .document(identity.fingerprint)
    )

    schedule_data = extraction.model_dump(mode="json")
    schedule_data["fingerprintData"] = identity.fingerprint_data.model_dump(mode="json")
    schedule_data.update(
        {
            "fingerprint": identity.fingerprint,
            "termKey": identity.term_key,
            "sourceFilename": source_filename,
            "createdAt": firestore.SERVER_TIMESTAMP,
        }
    )

    try:
        await schedule_ref.create(schedule_data)
        return True
    except AlreadyExists:
        return False
