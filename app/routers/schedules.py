"""Authenticated endpoint for importing Kuwait University schedule PDFs."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status

from ..auth import verify_token
from ..limiter import limiter
from ..schedule_repository import create_schedule_version
from ..services.schedule_extraction import extract_schedule
from ..services.schedule_fingerprint import InvalidFingerprintData, build_schedule_identity


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/schedules", tags=["schedules"])

MAX_SCHEDULE_FILE_SIZE = 10 * 1024 * 1024


async def _read_pdf(file: UploadFile) -> bytes:
    filename = file.filename or ""
    if Path(filename).suffix.lower() != ".pdf":
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Only PDF student schedules are supported.",
        )

    chunks: list[bytes] = []
    size = 0
    while chunk := await file.read(1024 * 1024):
        size += len(chunk)
        if size > MAX_SCHEDULE_FILE_SIZE:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="Schedule PDF exceeds the 10 MB limit.",
            )
        chunks.append(chunk)

    pdf_bytes = b"".join(chunks)
    if not pdf_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded PDF is empty.",
        )
    if b"%PDF-" not in pdf_bytes[:1024]:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="The uploaded file is not a valid PDF.",
        )
    return pdf_bytes


@router.post("/import")
@limiter.limit("5/minute")
async def import_schedule(
    request: Request,
    file: UploadFile = File(..., description="Kuwait University student schedule PDF"),
    uid: str = Depends(verify_token),
):
    pdf_bytes = await _read_pdf(file)
    filename = file.filename or "schedule.pdf"

    try:
        extraction = await extract_schedule(pdf_bytes, filename)
    except Exception as exc:
        logger.exception("Schedule extraction failed for user %s: %s", uid, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The schedule could not be analyzed. Please try again.",
        ) from exc

    extraction_payload = extraction.model_dump(mode="json")
    if not extraction.isValid:
        return {
            "stored": False,
            "duplicate": False,
            "fingerprint": None,
            "termKey": None,
            "schedule": extraction_payload,
        }

    try:
        identity = build_schedule_identity(extraction.fingerprintData)
    except InvalidFingerprintData as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"The schedule was recognized but its identity is incomplete: {exc}",
        ) from exc

    extraction.fingerprintData = identity.fingerprint_data
    created = await create_schedule_version(uid, extraction, identity, filename)

    return {
        "stored": created,
        "duplicate": not created,
        "fingerprint": identity.fingerprint,
        "termKey": identity.term_key,
        "schedule": extraction.model_dump(mode="json"),
    }
