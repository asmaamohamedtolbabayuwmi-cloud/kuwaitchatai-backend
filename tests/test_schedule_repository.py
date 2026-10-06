import asyncio
from unittest.mock import patch

from app.schedule_repository import SCHEDULE_CONTEXT_FIELDS, load_user_schedules


class _Snapshot:
    def __init__(self, data):
        self._data = data

    def to_dict(self):
        return self._data


class _Query:
    def __init__(self, documents):
        self._documents = documents

    async def stream(self):
        for document in self._documents:
            yield _Snapshot(document)


class _Collection:
    def __init__(self, documents, calls):
        self._documents = documents
        self._calls = calls

    def document(self, uid):
        self._calls.append(("document", uid))
        return self

    def collection(self, name):
        self._calls.append(("subcollection", name))
        return self

    def select(self, fields):
        self._calls.append(("select", tuple(fields)))
        return _Query(self._documents)


class _Client:
    def __init__(self, documents, calls):
        self._documents = documents
        self._calls = calls

    def collection(self, name):
        self._calls.append(("collection", name))
        return _Collection(self._documents, self._calls)


def test_load_user_schedules_is_uid_scoped_and_returns_every_version():
    calls = []
    documents = [
        {"termKey": "term-1", "courses": [{"courseCode": "101"}]},
        {"termKey": "term-2", "courses": [{"courseCode": "201"}]},
    ]
    client = _Client(documents, calls)

    with patch("app.schedule_repository.firestore_async.client", return_value=client):
        result = asyncio.run(load_user_schedules("user-123"))

    assert result == documents
    assert calls == [
        ("collection", "users"),
        ("document", "user-123"),
        ("subcollection", "schedules"),
        ("select", SCHEDULE_CONTEXT_FIELDS),
    ]
