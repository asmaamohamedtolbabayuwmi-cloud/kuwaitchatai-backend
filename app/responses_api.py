"""
app/responses_api.py

Calls the OpenAI Responses API with file_search and streams SSE events.

SSE event format (matches Flutter parser):
  data: {"type": "chunk",  "content": "<text>"}   — per token
  data: {"type": "done",   "citations": [...]}     — end of stream
  data: {"type": "error",  "message": "<reason>"}  — on failure

FileCitation fields (Responses API, Python SDK):
  annotation.file_id   — OpenAI file ID
  annotation.index     — character index in response text
  (filename is fetched separately from the Files API after streaming)
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import AsyncIterator

from .config import get_config
from .openai_client import get_openai_client

logger = logging.getLogger(__name__)


@dataclass
class Citation:
    file_id: str
    filename: str
    index: int


@dataclass
class StreamResult:
    full_text: str
    citations: list[Citation]


async def stream_response(
    messages: list[dict],   # [{"role": "user"|"assistant", "content": "..."}]
    system_prompt: str,
    tools: list[dict] | None = None,
) -> AsyncIterator[str]:
    """
    Async generator that yields SSE-formatted strings.

    Yields:
        SSE lines like `data: {...}\\n\\n`
    After the generator is exhausted the caller can read `.result`
    by awaiting the returned coroutine.

    NOTE: The generator accumulates the full text and citations internally,
    and yields a final "done" event with citations after the stream closes.
    """
    client = get_openai_client()
    config = get_config()

    input_messages = [
        {"role": "system", "content": system_prompt},
        *messages,
    ]

    full_text_parts: list[str] = []
    raw_citations: list[dict] = []   # file and URL citations
    usage: dict | None = None

    try:
        import traceback
        logger.info(f"DEBUG: Starting Responses API stream")

        # If tools is not provided, default to file_search with vector store
        if tools is None:
            tools = [
                {
                    "type": "file_search",
                    "vector_store_ids": [config.OPENAI_VECTOR_STORE_ID],
                }
            ]

        # Responses API streaming — async for loop over events
        async with client.responses.stream(
            model=config.OPENAI_MODEL,
            input=input_messages,
            tools=tools,
        ) as stream:
            async for event in stream:
                event_type = event.type

                # ── Text delta ──────────────────────────────────────────
                if event_type == "response.output_text.delta":
                    delta: str = event.delta or ""
                    if delta:
                        full_text_parts.append(delta)
                        yield (
                            "data: "
                            + json.dumps({"type": "chunk", "content": delta})
                            + "\n\n"
                        )

                # ── Completed: collect file_search annotations ──────────
                elif event_type == "response.completed":
                    response = event.response
                    response_usage = getattr(response, "usage", None)
                    if response_usage is not None:
                        usage = {
                            "prompt_tokens": getattr(response_usage, "input_tokens", 0),
                            "completion_tokens": getattr(response_usage, "output_tokens", 0),
                            "total_tokens": getattr(response_usage, "total_tokens", 0),
                        }
                    for output in getattr(response, "output", None) or []:
                        if getattr(output, "type", None) == "message":
                            for part in getattr(output, "content", None) or []:
                                if getattr(part, "type", None) == "output_text":
                                    for ann in (
                                        getattr(part, "annotations", None) or []
                                    ):
                                        if (
                                            getattr(ann, "type", None)
                                            == "file_citation"
                                        ):
                                            raw_citations.append(
                                                {
                                                    "file_id": ann.file_id,
                                                    "index": ann.index,
                                                }
                                            )
                                        elif (
                                            getattr(ann, "type", None)
                                            == "url_citation"
                                        ):
                                            raw_citations.append(
                                                {
                                                    "type": "url_citation",
                                                    "url": ann.url,
                                                    "title": getattr(ann, "title", ann.url),
                                                    "start_index": getattr(ann, "start_index", 0),
                                                    "end_index": getattr(ann, "end_index", 0),
                                                }
                                            )

    except Exception as exc:
        import traceback
        tb = traceback.format_exc()
        logger.error(f"DEBUG: Error streaming Responses API. Traceback:\n{tb}")
        logger.exception("Error streaming Responses API: %s", exc)
        yield (
            "data: "
            + json.dumps({"type": "error", "message": str(exc)})
            + "\n\n"
        )
        return

    # ── Resolve filenames (post-stream, non-blocking) ───────────────────
    file_citations = [
        item for item in raw_citations if item.get("type") != "url_citation"
    ]
    url_citations = [
        item for item in raw_citations if item.get("type") == "url_citation"
    ]
    citations = await _resolve_citations(file_citations)

    citation_list: list[dict] = [
        {
            "type": "file_citation",
            "file_id": c.file_id,
            "filename": c.filename,
            "index": c.index,
        }
        for c in citations
    ]
    seen_urls: set[str] = set()
    for citation in url_citations:
        if citation["url"] in seen_urls:
            continue
        seen_urls.add(citation["url"])
        citation_list.append(citation)

    if url_citations:
        links = []
        for citation in citation_list:
            if citation.get("type") == "url_citation":
                title = citation.get("title") or citation["url"]
                links.append(f"- [{title}]({citation['url']})")
        if links:
            sources = "\n\n**المصادر / Sources:**\n" + "\n".join(links)
            yield "data: " + json.dumps(
                {"type": "chunk", "content": sources}
            ) + "\n\n"

    done = {"type": "done", "citations": citation_list}
    if usage is not None:
        done["usage"] = usage
    yield "data: " + json.dumps(done) + "\n\n"


async def _resolve_citations(
    raw: list[dict],
) -> list[Citation]:
    """Deduplicate file IDs and fetch filenames from the Files API."""
    if not raw:
        return []

    client = get_openai_client()
    seen_ids: dict[str, dict] = {}
    for item in raw:
        if item["file_id"] not in seen_ids:
            seen_ids[item["file_id"]] = item

    async def fetch_name(file_id: str) -> tuple[str, str]:
        try:
            file_obj = await client.files.retrieve(file_id)
            return file_id, file_obj.filename
        except Exception:
            return file_id, file_id  # fallback to ID

    results = await asyncio.gather(*(fetch_name(fid) for fid in seen_ids))
    filename_map = dict(results)

    return [
        Citation(
            file_id=fid,
            filename=filename_map.get(fid, fid),
            index=seen_ids[fid]["index"],
        )
        for fid in seen_ids
    ]
