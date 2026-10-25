import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.responses_api import Citation, stream_response


class _FakeStream:
    def __init__(self, events):
        self.events = events

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for event in self.events:
            yield event


def test_file_search_results_are_returned_as_internal_evidence():
    captured = {}
    response = SimpleNamespace(
        usage=SimpleNamespace(input_tokens=10, output_tokens=4, total_tokens=14),
        output=[
            SimpleNamespace(
                type="file_search_call",
                results=[SimpleNamespace(text="تجدد الهوية عند تحقق شرط موثق")],
            ),
            SimpleNamespace(
                type="message",
                content=[
                    SimpleNamespace(
                        type="output_text",
                        annotations=[
                            SimpleNamespace(
                                type="file_citation", file_id="file-1", index=0
                            )
                        ],
                    )
                ],
            ),
        ],
    )
    events = [
        SimpleNamespace(type="response.output_text.delta", delta="إجابة"),
        SimpleNamespace(type="response.completed", response=response),
    ]

    class Responses:
        def stream(self, **kwargs):
            captured.update(kwargs)
            return _FakeStream(events)

    client = SimpleNamespace(responses=Responses())

    async def run():
        return [
            item
            async for item in stream_response(
                [{"role": "user", "content": "سؤال"}],
                "prompt",
                tools=[{"type": "file_search", "vector_store_ids": ["vs-test"]}],
            )
        ]

    with (
        patch("app.responses_api.get_openai_client", return_value=client),
        patch(
            "app.responses_api.get_config",
            return_value=SimpleNamespace(OPENAI_MODEL="test-model"),
        ),
        patch(
            "app.responses_api._resolve_citations",
            new=AsyncMock(return_value=[Citation("file-1", "policy.pdf", 0)]),
        ),
    ):
        output = asyncio.run(run())

    assert captured["include"] == ["file_search_call.results"]
    assert captured["tool_choice"] == "required"
    done = json.loads(output[-1][6:].strip())
    assert done["evidence"] == ["تجدد الهوية عند تحقق شرط موثق"]
    assert done["citations"][0]["filename"] == "policy.pdf"


def test_non_retrieval_request_omits_tool_choice_and_include():
    captured = {}
    response = SimpleNamespace(usage=None, output=[])
    events = [SimpleNamespace(type="response.completed", response=response)]

    class Responses:
        def stream(self, **kwargs):
            captured.update(kwargs)
            return _FakeStream(events)

    client = SimpleNamespace(responses=Responses())

    async def run():
        return [
            item
            async for item in stream_response(
                [{"role": "user", "content": "مش فاهم"}],
                "prompt",
                tools=[],
            )
        ]

    with (
        patch("app.responses_api.get_openai_client", return_value=client),
        patch(
            "app.responses_api.get_config",
            return_value=SimpleNamespace(OPENAI_MODEL="test-model"),
        ),
    ):
        output = asyncio.run(run())

    assert captured["tools"] == []
    assert "tool_choice" not in captured
    assert "include" not in captured
    assert json.loads(output[-1][6:].strip()) == {"type": "done", "citations": []}
