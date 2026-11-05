import asyncio
import json
import os
from pathlib import PurePosixPath
from urllib.parse import quote

from azure.core import MatchConditions
from azure.core.exceptions import (
    ResourceExistsError,
    ResourceModifiedError,
    ResourceNotFoundError,
)
from azure.identity.aio import DefaultAzureCredential
from azure.storage.blob.aio import BlobServiceClient


class AzureClaudeSessionStore:
    """
    Durable Claude Agent SDK transcript store.

    Each main session and subagent transcript is stored as JSONL in Azure
    Blob Storage. UUIDs provide idempotency and ETags protect concurrent
    updates across application instances.
    """

    def __init__(self):
        account_url = os.getenv("AZURE_STORAGE_ACCOUNT_URL")
        container_name = (
            os.getenv("CLAUDE_TRANSCRIPT_CONTAINER")
            or os.getenv("AZURE_STORAGE_CONTAINER")
        )

        if not account_url or not container_name:
            raise RuntimeError(
                "AZURE_STORAGE_ACCOUNT_URL and either "
                "CLAUDE_TRANSCRIPT_CONTAINER or AZURE_STORAGE_CONTAINER "
                "must be configured."
            )

        self.credential = DefaultAzureCredential()
        self.service = BlobServiceClient(
            account_url=account_url,
            credential=self.credential,
        )
        self.container = self.service.get_container_client(container_name)
        self._locks = {}

    def _blob_name(self, key):
        project_key = key["project_key"]
        session_id = key["session_id"]

        if not project_key or not session_id:
            raise ValueError("Transcript project_key and session_id are required.")

        if "/" in session_id or "\\" in session_id:
            raise ValueError("Invalid transcript session ID.")

        name = (
            "claude-transcripts/"
            f"{quote(project_key, safe='')}/"
            f"{quote(session_id, safe='')}"
        )

        subpath = key.get("subpath")
        if subpath:
            path = PurePosixPath(subpath)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("Invalid transcript subpath.")

            encoded_subpath = "/".join(
                quote(part, safe="") for part in path.parts
            )
            name += f"/{encoded_subpath}"

        return f"{name}.jsonl"

    def _lock_for(self, blob_name):
        lock = self._locks.get(blob_name)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[blob_name] = lock
        return lock

    @staticmethod
    def _serialize(entries):
        return "".join(
            json.dumps(entry, separators=(",", ":"), ensure_ascii=False) + "\n"
            for entry in entries
        ).encode("utf-8")

    @staticmethod
    def _deserialize(data):
        return [
            json.loads(line)
            for line in data.decode("utf-8").splitlines()
            if line.strip()
        ]

    async def append(self, key, entries):
        if not entries:
            return

        blob_name = self._blob_name(key)
        blob = self.container.get_blob_client(blob_name)

        async with self._lock_for(blob_name):
            for attempt in range(8):
                try:
                    try:
                        # Read the current version and its ETag.
                        properties = await blob.get_blob_properties()
                        etag = properties.etag

                        downloader = await blob.download_blob(
                            etag=etag,
                            match_condition=MatchConditions.IfNotModified,
                        )
                        existing_data = await downloader.readall()
                        existing_entries = self._deserialize(existing_data)

                        # SDK retries may resend entries. Deduplicate entries
                        # that have a stable UUID, preserving original order.
                        existing_uuids = {
                            item.get("uuid")
                            for item in existing_entries
                            if item.get("uuid")
                        }

                        new_entries = []
                        for entry in entries:
                            entry_uuid = entry.get("uuid")
                            if entry_uuid and entry_uuid in existing_uuids:
                                continue
                            new_entries.append(entry)
                            if entry_uuid:
                                existing_uuids.add(entry_uuid)

                        if not new_entries:
                            return

                        updated_data = self._serialize(
                            existing_entries + new_entries
                        )

                        await blob.upload_blob(
                            updated_data,
                            overwrite=True,
                            etag=etag,
                            match_condition=MatchConditions.IfNotModified,
                        )
                        return

                    except ResourceNotFoundError:
                        # First write for this transcript.
                        await blob.upload_blob(
                            self._serialize(entries),
                            overwrite=False,
                        )
                        return

                except (ResourceExistsError, ResourceModifiedError):
                    # Another process created or updated this transcript.
                    if attempt == 7:
                        raise
                    await asyncio.sleep(0.05 * (2 ** attempt))

    async def load(self, key):
        blob = self.container.get_blob_client(self._blob_name(key))

        try:
            downloader = await blob.download_blob()
            data = await downloader.readall()
            return self._deserialize(data)
        except ResourceNotFoundError:
            return None

    async def list_subkeys(self, key):
        prefix = self._blob_name(key).removesuffix(".jsonl") + "/"
        subkeys = []

        async for item in self.container.list_blobs(name_starts_with=prefix):
            if not item.name.endswith(".jsonl"):
                continue

            relative = item.name[len(prefix):-len(".jsonl")]
            if relative:
                subkeys.append(relative)

        return sorted(subkeys)

    async def close(self):
        await self.service.close()
        await self.credential.close()
