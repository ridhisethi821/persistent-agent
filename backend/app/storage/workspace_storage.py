import shutil
import tempfile
import os
import re
import uuid
from pathlib import Path, PurePosixPath

from app.storage.session_store import DATA_DIR, WORKSPACE_DIR


class LocalWorkspaceStorage:
    """Local filesystem storage for development."""

    def get_workspace_path(self, session: dict) -> Path:
        relative_path = Path(session["workspace_relpath"])

        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError("Invalid workspace path")

        workspace = (DATA_DIR / relative_path).resolve()
        allowed_root = WORKSPACE_DIR.resolve()

        if not workspace.is_relative_to(allowed_root):
            raise ValueError("Workspace is outside allowed directory")

        if not workspace.is_dir():
            raise FileNotFoundError("Workspace does not exist")

        return workspace

    def validate_filename(self, filename: str) -> str:
        if not isinstance(filename, str) or not filename or chr(92) in filename:
            raise ValueError("Invalid filename")

        relative = PurePosixPath(filename)
        if relative.is_absolute() or len(relative.parts) > 20:
            raise ValueError("Invalid filename")

        for part in relative.parts:
            if (
                part in {"", ".", ".."}
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,179}", part)
            ):
                raise ValueError("Invalid filename")

        return relative.as_posix()

    def read_file(self, session: dict, filename: str) -> bytes:
        filename = self.validate_filename(filename)
        workspace = self.get_workspace_path(session)
        path = workspace.joinpath(*PurePosixPath(filename).parts)

        if any(parent.is_symlink() for parent in [path, *path.parents] if parent != workspace.parent):
            raise ValueError("Symbolic links are not allowed")

        if not path.is_relative_to(workspace) or not path.is_file():
            raise FileNotFoundError("File not found")

        return path.read_bytes()

    def write_file(self, session: dict, filename: str, content, overwrite=False) -> int:
        filename = self.validate_filename(filename)
        workspace = self.get_workspace_path(session)
        destination = workspace.joinpath(*PurePosixPath(filename).parts)

        if not destination.is_relative_to(workspace):
            raise ValueError("Invalid destination")

        parent = destination.parent

        # Reject existing symlink ancestors before creating directories.
        current = parent
        while current != workspace:
            if current.is_symlink():
                raise ValueError("Symbolic links are not allowed")
            current = current.parent

        parent.mkdir(parents=True, exist_ok=True)

        # Recheck after directory creation before writing.
        current = parent
        while True:
            if current.is_symlink():
                raise ValueError("Symbolic links are not allowed")
            if current == workspace:
                break
            current = current.parent

        if destination.is_symlink():
            raise ValueError("Cannot overwrite a symbolic link")
        if destination.exists() and not overwrite:
            raise FileExistsError("File already exists")

        temporary = parent / f".upload-{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("xb") as output:
                content.seek(0)
                shutil.copyfileobj(content, output)
            size = temporary.stat().st_size
            temporary.replace(destination)
            return size
        finally:
            temporary.unlink(missing_ok=True)

    def list_files(self, session: dict) -> list[dict]:
        workspace = self.get_workspace_path(session)
        files = []

        for path in sorted(workspace.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(workspace)
            if any(part.startswith(".") for part in relative.parts):
                continue
            files.append({
                "filename": relative.as_posix(),
                "size_bytes": path.stat().st_size,
            })

        return files

    def delete_file(self, session: dict, filename: str) -> bool:
        filename = self.validate_filename(filename)
        workspace = self.get_workspace_path(session)
        path = workspace.joinpath(*PurePosixPath(filename).parts)

        if any(parent.is_symlink() for parent in [path, *path.parents] if parent != workspace.parent):
            raise ValueError("Symbolic links are not allowed")

        if not path.is_relative_to(workspace) or not path.is_file():
            return False

        path.unlink()
        return True

    def get_file_path(self, session: dict, filename: str) -> Path:
        filename = self.validate_filename(filename)
        workspace = self.get_workspace_path(session)
        path = workspace.joinpath(*PurePosixPath(filename).parts)

        if any(parent.is_symlink() for parent in [path, *path.parents] if parent != workspace.parent):
            raise ValueError("Symbolic links are not allowed")

        if not path.is_relative_to(workspace) or not path.is_file():
            raise FileNotFoundError("File not found")

        return path



def create_workspace_storage():
    backend = os.getenv("WORKSPACE_STORAGE_BACKEND", "local").lower()

    if backend == "local":
        return LocalWorkspaceStorage()

    if backend == "azure_blob":
        from app.storage.azure_blob_storage import AzureBlobWorkspaceStorage
        return AzureBlobWorkspaceStorage()

    raise ValueError(f"Unsupported workspace storage backend: {backend}")


workspace_storage = create_workspace_storage()


def close_workspace_storage():
    """Close the workspace storage client during application shutdown."""
    close = getattr(workspace_storage, "close", None)
    if close:
        close()
