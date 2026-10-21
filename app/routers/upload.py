"""
routers/upload.py

POST /upload

Accepts a multipart/form-data file, uploads it to OpenAI Files,
and attaches it to the shared Vector Store.

Request  : multipart/form-data  { file: <binary> }
Headers  : Authorization: Bearer <Firebase ID Token>
"""

import logging
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, status

from ..auth import verify_admin
from ..services.knowledge_file_notifications import (
    create_knowledge_notification_job,
    process_knowledge_notification_job,
)
from ..task_tracker import background_tasks
from ..vector_store import upload_file_to_vector_store

logger = logging.getLogger(__name__)
router = APIRouter()

ALLOWED_EXTENSIONS = {
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".md": "text/markdown",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".json": "application/json",
}

MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB

@router.post("/upload")
async def upload(
    file: UploadFile = File(..., description="File to upload to the knowledge base"),
    uid: str = Depends(verify_admin),
):
    """Upload a file to OpenAI Vector Store (admin only)."""
    if not file or not file.filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No file provided.",
        )

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported file extension '{ext}'. Allowed: {', '.join(ALLOWED_EXTENSIONS.keys())}",
        )

    mime_type = ALLOWED_EXTENSIONS[ext]

    temp_file_path = ""
    try:
        # Stream file to disk to avoid OOM, enforcing size limit
        with NamedTemporaryFile(delete=False, suffix=ext) as temp_file:
            temp_file_path = temp_file.name
            size = 0
            while chunk := await file.read(1024 * 1024):  # Read 1MB chunks
                size += len(chunk)
                if size > MAX_FILE_SIZE:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail="File exceeds the maximum allowed size of 50MB.",
                    )
                temp_file.write(chunk)

        if size == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Uploaded file is empty.",
            )

        vs_file = await upload_file_to_vector_store(temp_file_path, file.filename, mime_type)
        # Attachment is not indexing completion. Persist the follow-up before
        # returning, then poll outside the request so slow indexing cannot cause
        # an avoidable HTTP timeout. Startup recovery resumes unfinished jobs.
        notification_status = "queued"
        try:
            await create_knowledge_notification_job(vs_file.file_id, vs_file.filename)
            background_tasks.create_task(
                process_knowledge_notification_job(vs_file.file_id)
            )
        except Exception as notification_exc:
            # The OpenAI upload remains valid even if notification bookkeeping
            # is temporarily unavailable. Operators can reconcile it from logs.
            notification_status = "scheduling_failed"
            logger.exception(
                "Knowledge notification scheduling failed for %s: %s",
                vs_file.file_id,
                notification_exc,
            )

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Upload failed for admin %s: %s", uid, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Upload failed: {exc}",
        ) from exc
    finally:
        if temp_file_path and os.path.exists(temp_file_path):
            os.remove(temp_file_path)

    return {
        "file_id": vs_file.file_id,
        "filename": vs_file.filename,
        "status": "success",
        "indexing_status": vs_file.status,
        "notification_status": notification_status,
    }
