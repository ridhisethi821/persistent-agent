
import os
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient
from azure.core.exceptions import ResourceNotFoundError, ResourceExistsError


class AzureBlobWorkspaceStorage:
    """Durable, session-isolated workspace storage using Azure Blob Storage."""

    def __init__(self):
        account_url = os.getenv("AZURE_STORAGE_ACCOUNT_URL")
        container_name = os.getenv("AZURE_STORAGE_CONTAINER")

        if not account_url or not container_name:
            raise RuntimeError(
                "AZURE_STORAGE_ACCOUNT_URL and "
                "AZURE_STORAGE_CONTAINER must be configured"
            )

        self.container_name = container_name
        self.credential = DefaultAzureCredential()
        self.service = BlobServiceClient(
            account_url=account_url,
            credential=self.credential,
        )
        self.container = self.service.get_container_client(container_name)

    def validate_filename(self, filename: str) -> str:
        from pathlib import PurePosixPath
        import re

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

    def _blob_name(self, session: dict, filename: str) -> str:
        filename = self.validate_filename(filename)
        session_id = session["id"]

        if not session_id or "/" in session_id or "\\" in session_id:
            raise ValueError("Invalid session ID")

        return f"workspaces/{session_id}/{filename}"

    def write_file(
        self,
        session: dict,
        filename: str,
        content,
        overwrite=False,
    ) -> int:
        blob = self.container.get_blob_client(
            self._blob_name(session, filename)
        )

        content.seek(0)

        try:
            blob.upload_blob(
                content,
                overwrite=overwrite,
                max_concurrency=1,
            )
        except ResourceExistsError:
            raise FileExistsError("File already exists")

        content.seek(0, 2)
        size = content.tell()
        content.seek(0)

        return size

    def read_file(self, session: dict, filename: str) -> bytes:
        blob = self.container.get_blob_client(
            self._blob_name(session, filename)
        )

        try:
            return blob.download_blob().readall()
        except ResourceNotFoundError:
            raise FileNotFoundError("File not found")

    def list_files(self, session: dict) -> list[dict]:
        prefix = f"workspaces/{session['id']}/"
        files = []

        for blob in self.container.list_blobs(name_starts_with=prefix):
            filename = blob.name[len(prefix):]
            if not filename or any(part.startswith(".") for part in filename.split("/")):
                continue
            try:
                filename = self.validate_filename(filename)
            except ValueError:
                continue

            files.append({
                "filename": filename,
                "size_bytes": blob.size or 0,
            })

        return sorted(files, key=lambda item: item["filename"])

    def delete_file(self, session: dict, filename: str) -> bool:
        blob = self.container.get_blob_client(
            self._blob_name(session, filename)
        )
        try:
            blob.delete_blob()
            return True
        except ResourceNotFoundError:
            return False

    def close(self):
        self.service.close()
        self.credential.close()

