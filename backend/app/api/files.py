
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile, File
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from app.storage.session_store_backend import get_session
from app.storage.workspace_storage import workspace_storage

router = APIRouter(tags=["files"])

MAX_FILE_SIZE = 20 * 1024 * 1024
ALLOWED_EXTENSIONS = {
    ".pdf", ".docx", ".xlsx", ".csv", ".txt",
    ".png", ".jpg", ".jpeg",
}


async def get_session_or_404(session_id: str) -> dict:
    session = await run_in_threadpool(get_session, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@router.post("/sessions/{session_id}/files")
async def upload_file(
    session_id: str,
    file: UploadFile = File(...),
):
    session = await get_session_or_404(session_id)

    try:
        filename = workspace_storage.validate_filename(file.filename or "")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid filename")

    if Path(filename).suffix.lower() not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Unsupported file type")

    try:
        with tempfile.SpooledTemporaryFile(
            max_size=2 * 1024 * 1024,
            mode="w+b",
        ) as content:
            total_size = 0

            while chunk := await file.read(1024 * 1024):
                total_size += len(chunk)

                if total_size > MAX_FILE_SIZE:
                    raise HTTPException(
                        status_code=413,
                        detail="File exceeds the 20 MB limit",
                    )

                content.write(chunk)

            if total_size == 0:
                raise HTTPException(
                    status_code=400,
                    detail="Empty files are not allowed",
                )

            content.seek(0)

            try:
                size = await run_in_threadpool(
                    workspace_storage.write_file,
                    session, filename, content
                )
            except FileExistsError:
                raise HTTPException(
                    status_code=409,
                    detail="File already exists. Rename it before uploading.",
                )

            return {
                "filename": filename,
                "size_bytes": size,
                "message": "File uploaded successfully",
            }
    finally:
        await file.close()


@router.get("/sessions/{session_id}/files")
async def list_files(session_id: str):
    session = await get_session_or_404(session_id)
    return {
        "session_id": session_id,
        "files": await run_in_threadpool(workspace_storage.list_files, session),
    }


@router.get("/sessions/{session_id}/files/{filename:path}")
async def download_file(session_id: str, filename: str):
    session = await get_session_or_404(session_id)

    try:
        content = await run_in_threadpool(
            workspace_storage.read_file, session, filename
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid filename")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found")

    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{Path(filename).name}"'
        },
    )
