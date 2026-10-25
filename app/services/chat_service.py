"""
app/services/chat_service.py

Service layer for chat orchestration.
"""

import asyncio
import json
import logging
import time
from typing import AsyncIterator

from ..prompts import get_system_prompt, get_web_search_prompt
from .query_router import (
    QueryIntent,
    classify_query,
    ku_web_search_tool,
    should_fallback_to_web,
)
from .missed_query_service import analyze_and_log_missed_query
from .academic_calendar import (
    calendar_prompt_context,
    format_calendar_answer,
    is_english_message,
    lookup_official_calendar,
    resolve_calendar_query,
)

from ..chat_repository import (
    load_recent_messages,
    save_assistant_message,
    save_user_message,
)
from ..config import get_config
from ..history_builder import build_history
from ..responses_api import stream_response
from ..schedule_repository import load_user_schedules
from .schedule_context import build_schedule_context
from .grounding import missing_evidence_answer, sanitize_university_id_answer
from ..task_tracker import background_tasks
from ..logger import chat_id_var, uid_var

logger = logging.getLogger(__name__)

PERSISTENCE_TIMEOUT_SECONDS = 3.0


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

    # Define the bounded write now, but start it only after history is read.
    # Otherwise the write can win the race and the current turn is both loaded
    # from Firestore and manually appended below.
    async def safe_save_user_message() -> bool:
        db_start = time.perf_counter()
        try:
            await asyncio.wait_for(
                save_user_message(
                    uid,
                    session_id,
                    message,
                    correlation_id=correlation_id,
                ),
                timeout=PERSISTENCE_TIMEOUT_SECONDS,
            )
            logger.info("Saved user message for session %s in %.3fs", session_id, time.perf_counter() - db_start)
            return True
        except asyncio.TimeoutError:
            logger.error(
                "Timed out saving user message for session %s after %.1fs",
                session_id,
                PERSISTENCE_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            # Do not retry an ambiguous commit: a retry with a random document
            # ID could duplicate the user turn if Firestore committed remotely.
            logger.error("Failed to save user message for session %s: %s", session_id, exc)
        return False

    # 1. Load history and every stored schedule version concurrently.
    db_load_start = time.perf_counter()
    async def safe_load_schedules() -> list[dict] | None:
        try:
            return await load_user_schedules(uid)
        except Exception as exc:
            logger.exception("Failed to load schedules for user %s: %s", uid, exc)
            return None

    try:
        raw_messages, schedules = await asyncio.gather(
            load_recent_messages(uid, session_id, max_docs),
            safe_load_schedules(),
        )
    except Exception:
        # Preserve best-effort persistence when a context read fails.
        await safe_save_user_message()
        raise

    user_message_saved = False
    db_latency = time.perf_counter() - db_load_start
    logger.info(
        "Firestore context load latency for session %s: %.3fs (schedules=%s)",
        session_id,
        db_latency,
        "unavailable" if schedules is None else len(schedules),
    )

    # Persist asynchronously after the history snapshot is complete. This keeps
    # the current request out of raw_messages without text-based deduplication,
    # so legitimate repeated questions remain intact.
    # 2. Build token-limited context (returns chronological list)
    history = build_history(raw_messages, config.HISTORY_TOKEN_BUDGET)

    # Convert to OpenAI input format
    history_dicts = [{"role": m.role, "content": m.content} for m in history]
    # Append the current user message at the end
    history_dicts.append({"role": "user", "content": message})

    # 3. Build system prompt with a compact structured view of all schedules.
    schedule_context = build_schedule_context(schedules)
    schedule_colleges = {
        str((schedule.get("student") or {}).get("college") or "").strip()
        for schedule in (schedules or [])
        if str((schedule.get("student") or {}).get("college") or "").strip()
    }
    schedule_college = next(iter(schedule_colleges)) if len(schedule_colleges) == 1 else None
    calendar_query = resolve_calendar_query(
        message,
        raw_messages,
        schedule_college=schedule_college,
    )
    calendar_resolution = None
    academic_calendar_context = (
        calendar_prompt_context(calendar_query, calendar_resolution)
        if calendar_query
        else None
    )
    system_prompt = get_system_prompt(
        system_context,
        schedule_context,
        academic_calendar_context,
    )

    # 4. Current-information requests go straight to the web; static official
    # and uncertain university intent retain the File Search -> web fallback.
    intent = classify_query(message)
    use_web_search = intent is QueryIntent.TIME_SENSITIVE_UNIVERSITY
    use_file_search = intent in {
        QueryIntent.OFFICIAL_UNIVERSITY,
        QueryIntent.UNCERTAIN,
    }
    tools = []
    if use_file_search:
        tools.append({
            "type": "file_search",
            "vector_store_ids": [config.OPENAI_VECTOR_STORE_ID],
        })

    # 5. Stream to client, collect result for Firestore
    accumulated_text: list[str] = []
    accumulated_citations: list[dict] = []
    token_usage = None
    ttft: float | None = None
    file_search_attempted = False
    done_payload: dict | None = None
    assistant_persistence_attempted = False

    openai_start = time.perf_counter()

    def parse_sse(sse_line: str) -> dict | None:
        if not sse_line.startswith("data: "):
            return None
        try:
            return json.loads(sse_line[6:].strip())
        except json.JSONDecodeError as exc:
            logger.warning("Failed to decode SSE line: %s", exc)
            return None

    def public_done(payload: dict) -> str:
        public_payload = {key: value for key, value in payload.items() if key != "evidence"}
        return "data: " + json.dumps(public_payload) + "\n\n"

    async def persist_assistant_message() -> None:
        nonlocal assistant_persistence_attempted
        if assistant_persistence_attempted:
            return
        final_text = "".join(accumulated_text).strip()
        if not final_text:
            return
        assistant_persistence_attempted = True
        if not user_message_saved:
            logger.error(
                "Skipping assistant persistence because the user turn was not saved for session %s",
                session_id,
            )
            return
        try:
            await asyncio.wait_for(
                save_assistant_message(
                    uid=uid,
                    session_id=session_id,
                    content=final_text,
                    citations=accumulated_citations,
                ),
                timeout=PERSISTENCE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.error(
                "Timed out saving assistant message for session %s after %.1fs",
                session_id,
                PERSISTENCE_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.exception(
                "Failed to save assistant message for session %s: %s",
                session_id,
                exc,
            )

    # Start persistence after the history snapshot.  Yield the route status
    # first so a slow Firestore commit never delays the initial SSE response.
    user_save_task = asyncio.create_task(safe_save_user_message())
    background_tasks.tasks.add(user_save_task)
    user_save_task.add_done_callback(background_tasks.tasks.discard)

    try:
        initial_status = (
            "searching_ku_web"
            if calendar_query is not None or use_web_search
            else "searching_knowledge"
            if use_file_search
            else "generating_answer"
        )
        yield "data: " + json.dumps(
            {"type": "status", "status": initial_status}
        ) + "\n\n"
        user_message_saved = await asyncio.shield(user_save_task)

        if calendar_query is not None:
            calendar_resolution = await lookup_official_calendar(calendar_query)
            answer = format_calendar_answer(
                calendar_query,
                calendar_resolution,
                english=is_english_message(message),
            )
            citations = [
                {
                    "type": "url_citation",
                    "url": source.url,
                    "title": (
                        source.title_en
                        if is_english_message(message)
                        else source.title_ar
                    ),
                    "start_index": 0,
                    "end_index": 0,
                }
                for source in calendar_resolution.citation_sources
            ]
            accumulated_text.append(answer)
            accumulated_citations.extend(citations)
            yield "data: " + json.dumps(
                {"type": "chunk", "content": answer}
            ) + "\n\n"
            done_payload = {"type": "done", "citations": citations}
        elif use_web_search:
            web_text: list[str] = []
            web_citations: list[dict] = []
            web_evidence: list[str] = []
            web_error: dict | None = None
            async for sse_line in stream_response(
                history_dicts,
                get_web_search_prompt(
                    system_context,
                    schedule_context,
                    academic_calendar_context,
                ),
                tools=[ku_web_search_tool()],
            ):
                if ttft is None and '"type": "chunk"' in sse_line:
                    ttft = time.perf_counter() - openai_start
                payload = parse_sse(sse_line)
                if payload is not None:
                    if payload.get("type") == "chunk":
                        web_text.append(payload.get("content", ""))
                    elif payload.get("type") == "done":
                        web_citations.extend(payload.get("citations", []))
                        web_evidence.extend(payload.get("evidence", []))
                        token_usage = payload.get("usage")
                        done_payload = payload
                    elif payload.get("type") == "error":
                        web_error = payload
            if web_error is not None:
                yield "data: " + json.dumps(web_error) + "\n\n"
                return
            answer = "".join(web_text).strip()
            if not web_citations:
                answer = missing_evidence_answer(message)
            else:
                answer, _ = sanitize_university_id_answer(
                    message, answer, web_evidence
                )
            accumulated_text.append(answer)
            accumulated_citations.extend(web_citations)
            yield "data: " + json.dumps(
                {"type": "chunk", "content": answer}
            ) + "\n\n"
        elif use_file_search:
            file_search_attempted = True
            buffered_lines: list[str] = []
            retrieved_text: list[str] = []
            retrieved_citations: list[dict] = []
            retrieved_evidence: list[str] = []
            retrieved_usage = None
            retrieved_done: dict | None = None

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
                    retrieved_evidence.extend(payload.get("evidence", []))
                    retrieved_usage = payload.get("usage")
                    retrieved_done = payload

            retrieved_answer = "".join(retrieved_text).strip()
            if not should_fallback_to_web(
                retrieved_answer,
                retrieved_citations,
            ):
                yield "data: " + json.dumps(
                    {"type": "status", "status": "generating_answer"}
                ) + "\n\n"
                safe_answer, was_sanitized = sanitize_university_id_answer(
                    message,
                    retrieved_answer,
                    retrieved_evidence,
                )
                accumulated_citations.extend(retrieved_citations)
                token_usage = retrieved_usage
                done_payload = retrieved_done or {
                    "type": "done",
                    "citations": retrieved_citations,
                }
                if was_sanitized:
                    accumulated_text.append(safe_answer)
                    yield "data: " + json.dumps(
                        {"type": "chunk", "content": safe_answer}
                    ) + "\n\n"
                else:
                    accumulated_text.extend(retrieved_text)
                    for sse_line in buffered_lines:
                        payload = parse_sse(sse_line)
                        if payload is not None and payload.get("type") == "done":
                            continue
                        if ttft is None and '"type": "chunk"' in sse_line:
                            ttft = time.perf_counter() - openai_start
                        yield sse_line
            else:
                yield "data: " + json.dumps(
                    {"type": "status", "status": "searching_ku_web"}
                ) + "\n\n"

                web_text: list[str] = []
                web_evidence: list[str] = []
                web_citations: list[dict] = []
                web_error: dict | None = None
                async for sse_line in stream_response(
                    history_dicts,
                    get_web_search_prompt(
                        system_context,
                        schedule_context,
                        academic_calendar_context,
                    ),
                    tools=[ku_web_search_tool()],
                ):
                    if ttft is None and '"type": "chunk"' in sse_line:
                        ttft = time.perf_counter() - openai_start
                    payload = parse_sse(sse_line)
                    if payload is not None:
                        if payload.get("type") == "chunk":
                            web_text.append(payload.get("content", ""))
                        elif payload.get("type") == "done":
                            web_citations.extend(payload.get("citations", []))
                            web_evidence.extend(payload.get("evidence", []))
                            token_usage = payload.get("usage")
                            done_payload = payload
                        elif payload.get("type") == "error":
                            web_error = payload
                if web_error is not None:
                    yield "data: " + json.dumps(web_error) + "\n\n"
                    return
                web_answer = "".join(web_text).strip()
                if not web_citations:
                    safe_answer = missing_evidence_answer(message)
                else:
                    safe_answer, _ = sanitize_university_id_answer(
                        message,
                        web_answer,
                        web_evidence,
                    )
                accumulated_text.append(safe_answer)
                accumulated_citations.extend(web_citations)
                yield "data: " + json.dumps(
                    {"type": "chunk", "content": safe_answer}
                ) + "\n\n"
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
                if payload is not None and payload.get("type") == "done":
                    done_payload = payload
                else:
                    yield sse_line

        # `done` means both messages have been committed in order (or that a
        # persistence failure was logged).  This makes immediate follow-ups see
        # the complete preceding turn and keeps `done` the final SSE event.
        await persist_assistant_message()
        if done_payload is not None:
            yield public_done(done_payload)

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

        # Persist partial text on disconnect/error.  Successful streams already
        # persisted before their final `done` event.
        final_text = "".join(accumulated_text).strip()
        if final_text and not assistant_persistence_attempted:
            await persist_assistant_message()

        # 8. Background Task: Missed Query Analytics
        was_file_search_attempted = file_search_attempted
        if (was_file_search_attempted and not accumulated_citations) or "عذراً" in final_text:
            background_tasks.create_task(
                analyze_and_log_missed_query(
                    query=message,
                    chat_id=session_id,
                    was_file_search_attempted=was_file_search_attempted,
                    model_used=config.OPENAI_MODEL
                )
            )
