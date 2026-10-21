"""
app/vector_store.py

Helpers for managing files in the shared OpenAI Vector Store.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import openai
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type

from .config import get_config
from .openai_client import get_openai_client
from .local_notification_test import enabled as local_notification_test_enabled

logger = logging.getLogger(__name__)


@dataclass
class VectorStoreFile:
    file_id: str
    filename: str
    created_at: int   # Unix timestamp (seconds)
    status: str       # "completed" | "in_progress" | "failed" | "cancelled"


@retry(
    wait=wait_exponential(multiplier=1, min=2, max=10),
    stop=stop_after_attempt(3),
    retry=retry_if_exception_type((openai.RateLimitError, openai.APIConnectionError)),
)
async def upload_file_to_vector_store(
    file_path: str,
    filename: str,
    mime_type: str,
) -> VectorStoreFile:
    """
    Upload a file to OpenAI Files API (purpose="assistants") and
    immediately attach it to the shared Vector Store.
    """
    if local_notification_test_enabled():
        from .local_openai_mock import create_file

        mocked = create_file(filename)
        return VectorStoreFile(
            file_id=mocked.file_id,
            filename=mocked.filename,
            created_at=mocked.created_at,
            status=mocked.status,
        )

    client = get_openai_client()
    config = get_config()
    vector_store_id = config.OPENAI_VECTOR_STORE_ID

    # Stream the file from disk using standard open()
    with open(file_path, "rb") as f:
        uploaded = await client.files.create(
            file=(filename, f, mime_type),
            purpose="assistants",
        )
    logger.info("Uploaded file to OpenAI Files: %s (%s)", uploaded.id, filename)

    vs_file = await client.vector_stores.files.create(
        vector_store_id,
        file_id=uploaded.id,
    )
    logger.info(
        "Attached file %s to Vector Store %s — status: %s",
        uploaded.id,
        vector_store_id,
        vs_file.status,
    )

    return VectorStoreFile(
        file_id=uploaded.id,
        filename=uploaded.filename,
        created_at=vs_file.created_at,
        status=vs_file.status,
    )


@retry(
    wait=wait_exponential(multiplier=1, min=2, max=10),
    stop=stop_after_attempt(3),
    retry=retry_if_exception_type((openai.RateLimitError, openai.APIConnectionError)),
)
async def list_vector_store_files(limit: int = 100, after: str | None = None) -> tuple[list[VectorStoreFile], str | None, bool]:
    """
    List files attached to the shared Vector Store (Paginated).
    Returns a tuple of (files, last_id, has_more).
    """
    if local_notification_test_enabled():
        from .local_openai_mock import list_files

        mocked = list_files()[:limit]
        return [VectorStoreFile(**vars(item)) for item in mocked], None, False

    client = get_openai_client()
    config = get_config()
    vector_store_id = config.OPENAI_VECTOR_STORE_ID

    if after:
        page = await client.vector_stores.files.list(vector_store_id, limit=limit, after=after)
    else:
        page = await client.vector_stores.files.list(vector_store_id, limit=limit)

    vs_files = list(page.data)
    
    async def fetch_filename(vs_file):
        try:
            file_obj = await client.files.retrieve(vs_file.id)
            return vs_file.id, file_obj.filename
        except Exception:
            logger.warning("Could not retrieve filename for %s", vs_file.id)
            return vs_file.id, vs_file.id

    # Gather filenames concurrently (Fix N+1)
    filename_results = await asyncio.gather(*(fetch_filename(f) for f in vs_files))
    filename_map = dict(filename_results)

    results = [
        VectorStoreFile(
            file_id=f.id,
            filename=filename_map.get(f.id, f.id),
            created_at=f.created_at,
            status=f.status,
        )
        for f in vs_files
    ]

    return results, page.last_id if page.has_more else None, page.has_more


@retry(
    wait=wait_exponential(multiplier=1, min=2, max=10),
    stop=stop_after_attempt(3),
    retry=retry_if_exception_type((openai.RateLimitError, openai.APIConnectionError)),
)
async def delete_file_from_vector_store(file_id: str) -> None:
    """
    Remove a file from the Vector Store, then delete it from OpenAI Files.
    """
    if local_notification_test_enabled():
        from .local_openai_mock import delete_file

        delete_file(file_id)
        return

    client = get_openai_client()
    config = get_config()
    vector_store_id = config.OPENAI_VECTOR_STORE_ID

    try:
        await client.vector_stores.files.delete(file_id=file_id, vector_store_id=vector_store_id)
        logger.info("Removed file %s from Vector Store %s", file_id, vector_store_id)

        await client.files.delete(file_id)
        logger.info("Deleted file %s from OpenAI Files", file_id)
    except openai.NotFoundError as e:
        logger.error("File %s not found during deletion: %s", file_id, e)
        raise ValueError(f"File {file_id} does not exist.") from e
    except Exception as e:
        logger.exception("Unexpected error deleting file %s: %s", file_id, e)
        raise
