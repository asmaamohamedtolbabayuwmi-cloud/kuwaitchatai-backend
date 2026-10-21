"""In-memory OpenAI/vector-store simulator for the isolated local workflow."""

from __future__ import annotations

import time
import uuid
from types import SimpleNamespace


_files: dict[str, dict[str, object]] = {}


def create_file(filename: str) -> SimpleNamespace:
    file_id = f"file-local-{uuid.uuid4().hex}"
    _files[file_id] = {
        "filename": filename,
        "created_at": int(time.time()),
        "polls": 0,
    }
    return SimpleNamespace(
        file_id=file_id,
        filename=filename,
        created_at=_files[file_id]["created_at"],
        status="in_progress",
    )


def list_files() -> list[SimpleNamespace]:
    return [
        SimpleNamespace(
            file_id=file_id,
            filename=str(value["filename"]),
            created_at=int(value["created_at"]),
            status="completed" if int(value["polls"]) >= 2 else "in_progress",
        )
        for file_id, value in _files.items()
    ]


def delete_file(file_id: str) -> None:
    if _files.pop(file_id, None) is None:
        raise ValueError(f"File {file_id} does not exist.")


class _MockVectorStoreFiles:
    async def retrieve(self, *, vector_store_id: str, file_id: str):
        del vector_store_id
        record = _files.get(file_id)
        if record is None:
            error = RuntimeError("mock file was deleted")
            error.status_code = 404
            raise error
        record["polls"] = int(record["polls"]) + 1
        status = "completed" if int(record["polls"]) >= 2 else "in_progress"
        return SimpleNamespace(status=status)


class LocalMockOpenAI:
    def __init__(self) -> None:
        self.vector_stores = SimpleNamespace(files=_MockVectorStoreFiles())


client = LocalMockOpenAI()
