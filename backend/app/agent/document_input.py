import base64
import mimetypes
import re
from pathlib import Path

from fastapi import HTTPException

from app.storage.workspace_storage import workspace_storage


MAX_DOCUMENT_SIZE = 20 * 1024 * 1024

SUPPORTED_MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


def get_document_block(session: dict, filename: str) -> dict:
    """Read a workspace PDF/image and create a native Claude content block."""

    try:
        filename = workspace_storage.validate_filename(filename)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid filename")

    extension = Path(filename).suffix.lower()
    media_type = SUPPORTED_MEDIA_TYPES.get(extension)

    if not media_type:
        raise HTTPException(
            status_code=400,
            detail="Only PDF, PNG and JPEG attachments are supported.",
        )

    try:
        content = workspace_storage.read_file(session, filename)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found")

    size = len(content)

    if size == 0 or size > MAX_DOCUMENT_SIZE:
        raise HTTPException(
            status_code=413,
            detail="Document must be between 1 byte and 20 MB.",
        )

    encoded = base64.b64encode(content).decode("ascii")

    if extension == ".pdf":
        return {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": encoded,
            },
        }

    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": encoded,
        },
    }

async def build_agent_prompt(
    prompt: str,
    session: dict,
    attachment_filename: str | None = None,
):
    if not attachment_filename:
        return prompt

    document_block = get_document_block(session, attachment_filename)

    async def messages():
        yield {
            "type": "user",
            "session_id": "",
            "parent_tool_use_id": None,
            "message": {
                "role": "user",
                "content": [
                    document_block,
                    {
                        "type": "text",
                        "text": (
                            f"{prompt}\n\n"
                            f"Attached workspace file: {attachment_filename}"
                        ),
                    },
                ],
            },
        }

    return messages()