import os
from pathlib import Path
from contextlib import asynccontextmanager

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.sessions import router as sessions_router
from app.api.chat import router as chat_router
from app.api.files import router as files_router

from app.storage.session_store_backend import (
    initialize_session_store,
    close_session_store,
)

from app.storage.claude_transcript_store import (
    initialize_claude_transcript_store,
    close_claude_transcript_store,
)

from app.storage.workspace_storage import close_workspace_storage
from app.agent.execution_tools import close_allocator


FRONTEND_DIST = Path(
    os.getenv("FRONTEND_DIST_DIR", str(PROJECT_ROOT / "frontend" / "dist"))
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        initialize_session_store()
        initialize_claude_transcript_store()
        yield
    finally:
        try:
            close_allocator()
        finally:
            try:
                await close_claude_transcript_store()
            finally:
                try:
                    close_workspace_storage()
                finally:
                    close_session_store()


app = FastAPI(
    title="Persistent Agent API",
    description="An agent platform with persistent workspaces.",
    version="0.2.0",
    lifespan=lifespan,
)

from app.auth import reviewer_auth
app.middleware("http")(reviewer_auth)


@app.get("/")
async def root():
    index_file = FRONTEND_DIST / "index.html"
    if index_file.is_file():
        return FileResponse(index_file)
    return {
        "message": "Persistent Agent API",
        "status": "running",
    }


@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "service": "persistent-agent-api",
        "version": "0.2.0",
    }


app.include_router(sessions_router)
app.include_router(chat_router)
app.include_router(files_router)

if FRONTEND_DIST.is_dir():
    app.mount(
        "/",
        StaticFiles(directory=str(FRONTEND_DIST), html=True),
        name="frontend",
    )
