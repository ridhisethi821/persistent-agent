
import json
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from app.agent.claude_service import send_message
from app.agent.streaming_service import stream_claude_message
from app.storage.session_store_backend import (
    get_session,
    update_claude_session_id,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["chat"])


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=10000)
    attachment_filename: str | None = Field(
        default=None,
        max_length=180,
    )


@router.post("/sessions/{session_id}/chat")
async def chat(session_id: str, request: ChatRequest):
    session = await run_in_threadpool(get_session, session_id)

    if session is None:
        raise HTTPException(
            status_code=404,
            detail="Session not found",
        )

    try:
        result = await send_message(
            prompt=request.message,
            session=session,
            previous_session_id=session.get("claude_session_id"),
            attachment_filename=request.attachment_filename,
        )

        claude_session_id = result["claude_session_id"]

        if claude_session_id:
            await run_in_threadpool(
                update_claude_session_id, session_id, claude_session_id
            )

        return {
            "session_id": session_id,
            "response": result["response"],
            "claude_session_id": claude_session_id,
        }

    except Exception:
        raise HTTPException(
            status_code=502,
            detail="Claude request failed.",
        )


def format_sse(event_type: str, data: dict) -> str:
    return (
        f"event: {event_type}\n"
        f"data: {json.dumps(data)}\n\n"
    )


@router.post("/sessions/{session_id}/chat/stream")
async def stream_chat(session_id: str, request: ChatRequest):
    session = await run_in_threadpool(get_session, session_id)

    if session is None:
        raise HTTPException(
            status_code=404,
            detail="Session not found",
        )

    async def event_generator():
        agent_stream = stream_claude_message(
            prompt=request.message,
            session=session,
            previous_session_id=session.get("claude_session_id"),
            attachment_filename=request.attachment_filename,
        )
        try:
            async for item in agent_stream:
                event_type = item["type"]

                if event_type in {"done", "error"}:
                    claude_id = item.get("claude_session_id")

                    if claude_id:
                        await run_in_threadpool(
                            update_claude_session_id, session_id, claude_id
                        )

                if event_type == "done":
                    claude_id = item.get("claude_session_id")

                    yield format_sse(
                        "done",
                        {
                            "session_id": session_id,
                            "claude_session_id": claude_id,
                        },
                    )
                else:
                    yield format_sse(event_type, item)

        except Exception:
            logger.exception("Streaming chat failed for session %s", session_id)
            yield format_sse(
                "error",
                {"message": "Claude streaming request failed. Check server logs."},
            )
        finally:
            await agent_stream.aclose()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
