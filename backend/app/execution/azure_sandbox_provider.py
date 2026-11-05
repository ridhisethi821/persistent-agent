import os
from io import BytesIO
from pathlib import PurePosixPath

from azure.identity import DefaultAzureCredential
from azure.containerapps.sandbox import SandboxGroupClient

from app.storage.workspace_storage import workspace_storage


class AzureSandboxProvider:
    """Azure Container Apps Sandbox execution provider."""

    name = "azure_sandbox"

    def __init__(self):
        self.subscription_id = os.getenv("AZURE_SUBSCRIPTION_ID")
        self.endpoint = os.getenv("AZURE_SANDBOX_ENDPOINT")
        self.default_resource_group = os.getenv(
            "AZURE_SANDBOX_RESOURCE_GROUP"
        )

        if not self.subscription_id:
            raise RuntimeError("AZURE_SUBSCRIPTION_ID is required")
        if not self.endpoint:
            raise RuntimeError("AZURE_SANDBOX_ENDPOINT is required")

        self.group_resource_groups = {}
        for item in os.getenv("AZURE_SANDBOX_GROUPS", "").split(","):
            item = item.strip()
            if not item:
                continue

            if ":" in item:
                group_name, resource_group = item.split(":", 1)
                group_name = group_name.strip()
                resource_group = resource_group.strip()

                if not group_name or not resource_group:
                    raise ValueError(
                        f"Invalid AZURE_SANDBOX_GROUPS entry: {item}"
                    )

                self.group_resource_groups[group_name] = resource_group

        self.credential = DefaultAzureCredential()
        self.cpu = os.getenv("AZURE_SANDBOX_CPU", "3000m")
        self.memory = os.getenv("AZURE_SANDBOX_MEMORY", "6144Mi")
        self.auto_suspend = int(
            os.getenv("AZURE_SANDBOX_AUTO_SUSPEND_SECONDS", "300")
        )
        self.disk_size = os.getenv("AZURE_SANDBOX_DISK_SIZE") or None
        self.disk_id = os.getenv("AZURE_SANDBOX_DISK_ID") or None
        self._clients = {}

    def _get_group_client(self, group_name):
        if not group_name:
            raise ValueError("Session has no assigned Sandbox Group")

        if group_name not in self._clients:
            resource_group = self.group_resource_groups.get(
                group_name,
                self.default_resource_group,
            )

            if not resource_group:
                raise RuntimeError(
                    f"No resource group configured for sandbox group "
                    f"'{group_name}'. Configure it in AZURE_SANDBOX_GROUPS."
                )

            self._clients[group_name] = SandboxGroupClient(
                endpoint=self.endpoint,
                credential=self.credential,
                subscription_id=self.subscription_id,
                resource_group=resource_group,
                sandbox_group=group_name,
            )

        return self._clients[group_name]

    def ensure_session(self, session):
        """Reconnect to an owned sandbox or safely recover/create one."""
        group = self._get_group_client(session.get("execution_host_id"))
        expected_token = session.get("execution_fencing_token")

        if expected_token is None:
            raise RuntimeError(
                "Cannot recover a sandbox without a session fencing token"
            )

        expected_token = str(expected_token)
        sandbox_id = session.get("execution_resource_id")

        if sandbox_id:
            sandbox = group.get_sandbox_client(sandbox_id)
            details = sandbox.get()
            labels = getattr(details, "labels", {}) or {}

            if labels.get("session_id") != session["id"]:
                raise RuntimeError(
                    "Stored sandbox does not belong to this session"
                )

            if str(labels.get("fencing_token", "")) != expected_token:
                raise RuntimeError(
                    "Stored sandbox fencing token does not match the session assignment"
                )

            state = getattr(details, "state", None)
            if state in {"Failed", "Deleting"}:
                raise RuntimeError(
                    f"Stored sandbox is in unrecoverable state: {state}"
                )

            sandbox.ensure_running()
            return sandbox_id

        # Recover only sandboxes belonging to this exact allocation.
        recoverable = []
        for item in group.list_sandboxes(
            labels={"session_id": session["id"]}
        ):
            labels = getattr(item, "labels", {}) or {}
            if (
                labels.get("session_id") != session["id"]
                or str(labels.get("fencing_token", "")) != expected_token
            ):
                continue

            state = getattr(item, "state", None)
            if state == "Failed":
                failed = group.get_sandbox_client(item.id)
                failed.begin_delete(
                    polling_timeout=120,
                    polling_interval=3,
                ).result()
                continue

            if state == "Deleting":
                continue

            recoverable.append(item)

        if recoverable:
            chosen = recoverable[0]
            sandbox = group.get_sandbox_client(chosen.id)
            sandbox.ensure_running()

            # Remove duplicate sandboxes from this same allocation only.
            for duplicate in recoverable[1:]:
                duplicate_client = group.get_sandbox_client(duplicate.id)
                duplicate_client.begin_delete(
                    polling_timeout=120,
                    polling_interval=3,
                ).result()

            return chosen.id

        options = {
            "cpu": self.cpu,
            "memory": self.memory,
            "auto_suspend_seconds": self.auto_suspend,
            "labels": {
                "session_id": session["id"],
                "fencing_token": expected_token,
            },
            "polling_timeout": 300,
        }
        if self.disk_size:
            options["disk_size"] = self.disk_size
        if self.disk_id:
            options["disk_id"] = self.disk_id

        sandbox = group.begin_create_sandbox(**options).result()
        return sandbox.sandbox_id

    @staticmethod
    def _sandbox_files(sandbox):
        """Recursively map workspace-relative names to sandbox paths."""
        files = {}
        visited = set()
        workspace_root = PurePosixPath("/workspace")

        def walk(directory):
            if directory in visited:
                return
            visited.add(directory)

            listing = sandbox.list_files(directory)
            if listing is None or not hasattr(listing, "entries"):
                raise RuntimeError(
                    f"Sandbox returned an invalid file listing for {directory}"
                )

            directory_path = PurePosixPath(directory)
            directory_relative = directory_path.relative_to(workspace_root)

            for entry in listing.entries:
                raw_path = getattr(entry, "path", None) or getattr(
                    entry, "name", None
                )
                if not raw_path:
                    raise RuntimeError(
                        f"Sandbox returned a file entry without a path in {directory}"
                    )

                candidate = PurePosixPath(raw_path)

                if candidate.is_absolute():
                    absolute = candidate
                elif (
                    directory_relative.parts
                    and candidate.parts[:len(directory_relative.parts)]
                    == directory_relative.parts
                ):
                    # Some listing responses may return paths relative to
                    # /workspace rather than relative to the listed directory.
                    absolute = workspace_root / candidate
                else:
                    absolute = directory_path / candidate

                if (
                    absolute == workspace_root
                    or not absolute.is_relative_to(workspace_root)
                ):
                    raise RuntimeError(
                        f"Sandbox returned an invalid workspace path: {raw_path}"
                    )

                relative = absolute.relative_to(workspace_root).as_posix()
                relative = workspace_storage.validate_filename(relative)

                if any(
                    part.startswith(".")
                    for part in PurePosixPath(relative).parts
                ):
                    continue

                if getattr(entry, "is_directory", False):
                    walk(absolute.as_posix())
                else:
                    files[relative] = absolute.as_posix()

        walk("/workspace")
        return files

    def _sync_to_sandbox(self, session, sandbox):
        """Reconcile all sandbox files with the persistent workspace."""
        persistent_files = {
            item["filename"]: item
            for item in workspace_storage.list_files(session)
        }
        sandbox_files = self._sandbox_files(sandbox)

        for filename, sandbox_path in sandbox_files.items():
            if filename not in persistent_files:
                sandbox.delete_file(sandbox_path)

        for filename in sorted(persistent_files):
            filename = workspace_storage.validate_filename(filename)
            content = workspace_storage.read_file(session, filename)
            sandbox.write_file(
                f"/workspace/{filename}",
                content,
                create_dirs=True,
            )

    def _sync_from_sandbox(self, session, sandbox):
        """Persist sandbox changes; perform deletions only after successful reads."""
        sandbox_files = self._sandbox_files(sandbox)

        # Read every file before changing persistent storage. A failed read
        # must not leave a partially reconciled workspace or trigger deletions.
        downloaded = {}
        for filename, sandbox_path in sandbox_files.items():
            filename = workspace_storage.validate_filename(filename)
            downloaded[filename] = sandbox.read_file(sandbox_path)

        for filename, content in downloaded.items():
            workspace_storage.write_file(
                session,
                filename,
                BytesIO(content),
                overwrite=True,
            )

        sandbox_names = set(downloaded)
        for item in workspace_storage.list_files(session):
            filename = item["filename"]
            if filename not in sandbox_names:
                workspace_storage.delete_file(session, filename)

    @staticmethod
    def _limit_output(value, max_bytes=64 * 1024):
        if value is None:
            return "", False
        if isinstance(value, bytes):
            encoded = value
        else:
            encoded = str(value).encode("utf-8", errors="replace")

        truncated = len(encoded) > max_bytes
        return encoded[:max_bytes].decode("utf-8", errors="ignore"), truncated

    def run_command(self, session, command, timeout=30):
        group = self._get_group_client(
            session.get("execution_host_id")
        )
        sandbox_id = session.get("execution_resource_id")

        if not sandbox_id:
            sandbox_id = self.ensure_session(session)

        sandbox = group.get_sandbox_client(sandbox_id)
        sandbox.ensure_running()

        self._sync_to_sandbox(session, sandbox)

        try:
            result = sandbox.exec(
                command,
                working_directory="/workspace",
                timeout=timeout,
            )
        except Exception as execution_error:
            try:
                self._sync_from_sandbox(session, sandbox)
            except Exception as sync_error:
                execution_error.add_note(
                    f"Workspace synchronization also failed: {sync_error}"
                )
            raise
        else:
            self._sync_from_sandbox(session, sandbox)

        stdout, stdout_truncated = self._limit_output(result.stdout)
        stderr, stderr_truncated = self._limit_output(result.stderr)

        return {
            "exit_code": result.exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "truncated": stdout_truncated or stderr_truncated,
            "container_id": sandbox_id,
        }

    def cleanup_failed_allocation(
        self, session, resource_id, fencing_token
    ):
        """Delete only sandboxes bearing this session's exact fencing token."""
        if fencing_token is None:
            raise ValueError("A fencing token is required for sandbox cleanup")

        group = self._get_group_client(session.get("execution_host_id"))
        candidates = set()

        if resource_id:
            candidates.add(resource_id)

        for item in group.list_sandboxes(
            labels={"session_id": session["id"]}
        ):
            labels = getattr(item, "labels", {}) or {}
            if (
                labels.get("session_id") == session["id"]
                and str(labels.get("fencing_token", "")) == str(fencing_token)
            ):
                candidates.add(item.id)

        for sandbox_id in candidates:
            sandbox = group.get_sandbox_client(sandbox_id)
            details = sandbox.get()
            labels = getattr(details, "labels", {}) or {}

            if (
                labels.get("session_id") != session["id"]
                or str(labels.get("fencing_token", "")) != str(fencing_token)
            ):
                continue

            if getattr(details, "state", None) == "Deleting":
                continue

            sandbox.begin_delete(
                polling_timeout=120,
                polling_interval=3,
            ).result()

    def stop_session(self, session_id):
        from app.storage.session_store_backend import get_session

        session = get_session(session_id)
        if not session:
            return

        sandbox_id = session.get("execution_resource_id")
        group_name = session.get("execution_host_id")

        if sandbox_id and group_name:
            self._get_group_client(group_name).get_sandbox_client(
                sandbox_id
            ).stop()

    def close(self):
        self._clients.clear()
        self.credential.close()
