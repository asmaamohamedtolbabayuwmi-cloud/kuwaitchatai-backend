"""
app/services/chat_service.py

Service layer for chat orchestration.
"""

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import AsyncIterator

from ..prompts import get_system_prompt, get_web_search_prompt
from .query_router import (
    ku_web_search_tool,
    should_fallback_to_web,
    should_use_file_search,
    web_search_status,
)
from .missed_query_service import analyze_and_log_missed_query

from ..chat_repository import (
    load_recent_messages,
    save_assistant_message,
    save_user_message,
)
from ..config import get_config
from ..history_builder import build_history
from ..responses_api import stream_response
from ..task_tracker import background_tasks
from ..logger import chat_id_var, uid_var

logger = logging.getLogger(__name__)


async def process_chat(
    uid: str,
    session_id: str,
    message: str,
    system_context: str | None = None,
    correlation_id: str | None = None,
) -> AsyncIterator[str]:
    """
    Handle the core business logic of generating a chat stream.
    Saves the user message, builds context, streams from OpenAI,
    and saves the final response.
    """
    config = get_config()
    
    # Set context variables for structured logging
    chat_id_var.set(session_id)
    uid_var.set(uid)

    if correlation_id:
        logger.info("Starting chat request with correlation_id: %s", correlation_id)

    # Calculate how many Firestore docs to fetch based on token budget
    # ~50 tokens per message on average → budget / 50 + small headroom
    max_docs = max(10, config.HISTORY_TOKEN_BUDGET // 50 + 5)

    # 1. Save user message concurrently
    async def safe_save_user_message():
        db_start = time.perf_counter()
        for attempt in range(3):
            try:
                await save_user_message(uid, session_id, message, correlation_id=correlation_id)
                logger.info("Saved user message for session %s in %.3fs", session_id, time.perf_counter() - db_start)
                break
            except Exception as e:
                if attempt == 2:
                    logger.error("Failed to save user message after 3 attempts for session %s: %s", session_id, e)
                else:
                    await asyncio.sleep(1)

    background_tasks.create_task(safe_save_user_message())

    # 2. Load history
    db_load_start = time.perf_counter()
    raw_messages = await load_recent_messages(uid, session_id, max_docs)
    db_latency = time.perf_counter() - db_load_start
    logger.info("Firestore load latency for session %s: %.3fs", session_id, db_latency)

    # 3. Build token-limited context (returns chronological list)
    history = build_history(raw_messages, config.HISTORY_TOKEN_BUDGET)

    # Convert to OpenAI input format
    history_dicts = [{"role": m.role, "content": m.content} for m in history]
    # Append the current user message at the end
    history_dicts.append({"role": "user", "content": message})

    # 4. Build system prompt
    system_prompt = get_system_prompt(system_context)

    # 5. Route: Decide if we need File Search
    use_file_search = should_use_file_search(message)
    tools = []
    if use_file_search:
        tools.append({
            "type": "file_search",
            "vector_store_ids": [config.OPENAI_VECTOR_STORE_ID],
        })

    # 6. Stream to client, collect result for Firestore
    accumulated_text: list[str] = []
    accumulated_citations: list[dict] = []
    token_usage = None
    ttft: float | None = None

    openai_start = time.perf_counter()

    def parse_sse(sse_line: str) -> dict | None:
        if not sse_line.startswith("data: "):
            return None
        try:
            return json.loads(sse_line[6:].strip())
        except json.JSONDecodeError as exc:
            logger.warning("Failed to decode SSE line: %s", exc)
            return None

    try:
        if use_file_search:
            buffered_lines: list[str] = []
            retrieved_text: list[str] = []
            retrieved_citations: list[dict] = []
            retrieved_usage = None

            async for sse_line in stream_response(
                history_dicts, system_prompt, tools=tools
            ):
                buffered_lines.append(sse_line)
                payload = parse_sse(sse_line)
                if payload is None:
                    continue
                if payload.get("type") == "chunk":
                    retrieved_text.append(payload.get("content", ""))
                elif payload.get("type") == "done":
                    retrieved_citations.extend(payload.get("citations", []))
                    retrieved_usage = payload.get("usage")

            retrieved_answer = "".join(retrieved_text).strip()
            if not should_fallback_to_web(
                retrieved_answer,
                retrieved_citations,
            ):
                accumulated_text.extend(retrieved_text)
                accumulated_citations.extend(retrieved_citations)
                token_usage = retrieved_usage
                for sse_line in buffered_lines:
                    if ttft is None and '"type": "chunk"' in sse_line:
                        ttft = time.perf_counter() - openai_start
                    yield sse_line
            else:
                status = web_search_status(message)
                yield "data: " + json.dumps(
                    {"type": "chunk", "content": status}
                ) + "\n\n"

                async for sse_line in stream_response(
                    history_dicts,
                    get_web_search_prompt(system_context),
                    tools=[ku_web_search_tool()],
                ):
                    if ttft is None and '"type": "chunk"' in sse_line:
                        ttft = time.perf_counter() - openai_start
                    payload = parse_sse(sse_line)
                    if payload is not None:
                        if payload.get("type") == "chunk":
                            accumulated_text.append(payload.get("content", ""))
                        elif payload.get("type") == "done":
                            accumulated_citations.extend(
                                payload.get("citations", [])
                            )
                            token_usage = payload.get("usage")
                    yield sse_line
        else:
            async for sse_line in stream_response(
                history_dicts, system_prompt, tools=[]
            ):
                if ttft is None and '"type": "chunk"' in sse_line:
                    ttft = time.perf_counter() - openai_start
                payload = parse_sse(sse_line)
                if payload is not None:
                    if payload.get("type") == "chunk":
                        accumulated_text.append(payload.get("content", ""))
                    elif payload.get("type") == "done":
                        accumulated_citations.extend(payload.get("citations", []))
                        token_usage = payload.get("usage")
                yield sse_line
            
    except asyncio.CancelledError:
        logger.warning("Client disconnected during stream for session %s", session_id)
        raise
    except Exception as e:
        logger.error("Error during OpenAI stream for session %s: %s", session_id, e)
        yield f'data: {{"type": "error", "message": "{e}"}}\n\n'
    finally:
        openai_latency = time.perf_counter() - openai_start
        
        # Estimate cost (rough approx for gpt-4o-mini)
        est_cost = 0.0
        if token_usage:
            # gpt-4o-mini: $0.150 / 1M input, $0.600 / 1M output
            in_cost = (token_usage.get("prompt_tokens", 0) / 1_000_000) * 0.150
            out_cost = (token_usage.get("completion_tokens", 0) / 1_000_000) * 0.600
            est_cost = in_cost + out_cost
            
        logger.info(
            "OpenAI stream completed.",
            extra={
                "operational_metadata": {
                    "ttft": ttft,
                    "latency": openai_latency,
                    "model": config.OPENAI_MODEL,
                    "token_usage": token_usage,
                    "estimated_cost": round(est_cost, 6)
                }
            }
        )
        
        # 7. Background Task: Async Database Write
        final_text = "".join(accumulated_text).strip()
        if final_text:
            background_tasks.create_task(
                save_assistant_message(
                    uid=uid,
                    session_id=session_id,
                    content=final_text,
                    citations=accumulated_citations,
                )
            )
        
        # 8. Background Task: Missed Query Analytics
        was_file_search_attempted = use_file_search
        if (was_file_search_attempted and not accumulated_citations) or "عذراً" in final_text:
            background_tasks.create_task(
                analyze_and_log_missed_query(
                    query=message,
                    chat_id=session_id,
                    was_file_search_attempted=was_file_search_attempted,
                    model_used=config.OPENAI_MODEL
                )
            )
