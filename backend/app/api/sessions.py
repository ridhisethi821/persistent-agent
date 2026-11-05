import os
from pathlib import Path

from claude_agent_sdk import get_session_messages
from claude_agent_sdk._internal.sessions import get_session_messages_from_store

from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from app.storage.claude_transcript_store import get_claude_transcript_store
from app.storage.session_store_backend import (
    create_session,
    get_session,
    list_sessions,
)

router = APIRouter(prefix="/sessions", tags=["sessions"])


class CreateSessionRequest(BaseModel):
    title: str = Field(default="New chat", min_length=1, max_length=120)


@router.post("", status_code=201)
async def create_new_session(request: CreateSessionRequest):
    provider = os.getenv("EXECUTION_PROVIDER", "local_docker")
    return await run_in_threadpool(
        create_session, request.title, execution_provider=provider
    )


@router.get("")
async def get_all_sessions():
    return {"sessions": await run_in_threadpool(list_sessions)}


@router.get("/{session_id}")
async def get_existing_session(session_id: str):
    session = await run_in_threadpool(get_session, session_id)

    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    return session


@router.get("/{session_id}/messages")
async def get_session_history(session_id: str):
    """Return the readable conversation history for a session."""
    session = await run_in_threadpool(get_session, session_id)

    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    project_root = str(Path(__file__).resolve().parents[3])
    transcript_store = get_claude_transcript_store()
    claude_session_id = session.get("claude_session_id")

    if not claude_session_id:
        history = []
    elif transcript_store is not None:
        history = await get_session_messages_from_store(
            transcript_store,
            claude_session_id,
            directory=project_root,
        )
    else:
        history = await run_in_threadpool(
            get_session_messages,
            claude_session_id,
            directory=project_root,
        )

    messages = []

    for item in history:
        raw = item.message or {}
        content = raw.get("content", "")
        parts = []

        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))

        text = "\n".join(part for part in parts if part).strip()

        if text:
            messages.append({
                "id": item.uuid,
                "role": item.type,
                "content": text,
            })

    return {"messages": messages}
