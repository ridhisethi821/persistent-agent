import os
import uuid
from pathlib import Path

import docker
from docker.errors import NotFound

from app.storage.session_store import DATA_DIR, WORKSPACE_DIR


IMAGE_NAME = "persistent-agent-executor:0.1"
CONTAINER_MEMORY = 512 * 1024 * 1024
CONTAINER_CPUS = 1_000_000_000
MAX_OUTPUT_BYTES = 64 * 1024


class DockerExecutor:
    """Manage one isolated Docker execution container per application session."""

    def __init__(self):
        self.client = docker.from_env()
        self.client.ping()

    def _get_workspace(self, session: dict) -> Path:
        relative_path = Path(session["workspace_relpath"])

        if relative_path.is_absolute():
            raise ValueError("Workspace path must be relative.")

        workspace = (DATA_DIR / relative_path).resolve()
        allowed_root = WORKSPACE_DIR.resolve()

        try:
            workspace.relative_to(allowed_root)
        except ValueError:
            raise ValueError("Workspace path is outside the workspace directory.")

        if not workspace.is_dir():
            raise FileNotFoundError("Session workspace does not exist.")

        return workspace

    def _get_docker_mount_workspace(self, session: dict) -> Path:
        """
        Resolve the workspace path as seen by the Docker daemon.
        When the API runs inside a container, its filesystem path may
        differ from the host path understood by the Docker daemon.
        """
        host_data_dir = os.getenv("DOCKER_WORKSPACE_HOST_DATA_DIR")
        if not host_data_dir:
            return self._get_workspace(session)

        relative_path = Path(session["workspace_relpath"])
        if relative_path.is_absolute():
            raise ValueError("Workspace path must be relative.")

        host_root = Path(host_data_dir).expanduser().resolve()
        host_workspace = (host_root / relative_path).resolve()

        try:
            host_workspace.relative_to(host_root)
        except ValueError:
            raise ValueError("Host workspace path is outside the configured data directory.")

        # Do not check host_workspace.is_dir() here: this code may run
        # inside an API container, where the Mac host path is not visible.
        # Docker validates the host-side bind source when starting the container.
        return host_workspace

    def _container_name(self, session_id: str) -> str:
        # Only accept valid application UUIDs.
        normalized_id = str(uuid.UUID(session_id))
        return f"persistent-agent-{normalized_id}"

    def get_or_create_container(self, session: dict):
        session_id = session["id"]
        container_name = self._container_name(session_id)
        # Validate the application workspace, then use the host-visible
        # path for Docker bind mounts when configured.
        self._get_workspace(session)
        workspace = self._get_docker_mount_workspace(session)

        try:
            container = self.client.containers.get(container_name)
            container.reload()

            if container.status != "running":
                container.start()
                container.reload()

            return container

        except NotFound:
            pass

        # Match the local macOS user's UID/GID so the container can
        # write to the mounted workspace without running as root.
        local_user = f"{os.getuid()}:{os.getgid()}"

        container = self.client.containers.create(
            image=IMAGE_NAME,
            name=container_name,
            command=["sleep", "infinity"],
            detach=True,
            user=local_user,
            working_dir="/workspace",
            environment={
                "HOME": "/tmp",
                "TMPDIR": "/tmp",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            volumes={
                str(workspace): {
                    "bind": "/workspace",
                    "mode": "rw",
                }
            },
            mem_limit=CONTAINER_MEMORY,
            memswap_limit=CONTAINER_MEMORY,
            nano_cpus=CONTAINER_CPUS,
            pids_limit=128,
            read_only=True,
            tmpfs={
                "/tmp": "rw,noexec,nosuid,size=64m",
            },
            network_disabled=True,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            init=True,
            restart_policy={"Name": "no"},
            labels={
                "app": "persistent-agent",
                "session_id": str(session_id),
            },
        )

        container.start()
        return container

    def run_command(
        self,
        session: dict,
        command: str,
        timeout_seconds: int = 30,
    ) -> dict:
        if not command or not command.strip():
            raise ValueError("Command cannot be empty.")

        timeout_seconds = max(1, min(timeout_seconds, 120))

        container = self.get_or_create_container(session)

        if container.status != "running":
            container.reload()

        if container.status != "running":
            raise RuntimeError("Execution container is not running.")

        result = container.exec_run(
            cmd=[
                "timeout",
                "--signal=TERM",
                "--kill-after=5s",
                f"{timeout_seconds}s",
                "/bin/bash",
                "-lc",
                command,
            ],
            workdir="/workspace",
            user=f"{os.getuid()}:{os.getgid()}",
            demux=True,
        )

        stdout, stderr = result.output or (b"", b"")

        stdout = stdout or b""
        stderr = stderr or b""

        truncated = (
            len(stdout) > MAX_OUTPUT_BYTES
            or len(stderr) > MAX_OUTPUT_BYTES
        )

        return {
            "exit_code": result.exit_code,
            "stdout": stdout[:MAX_OUTPUT_BYTES].decode(
                "utf-8", errors="replace"
            ),
            "stderr": stderr[:MAX_OUTPUT_BYTES].decode(
                "utf-8", errors="replace"
            ),
            "truncated": truncated,
            "container_id": container.id,
        }

    def stop_container(self, session_id: str) -> bool:
        container_name = self._container_name(session_id)

        try:
            container = self.client.containers.get(container_name)
            container.stop(timeout=5)
            return True
        except NotFound:
            return False

    def close(self):
        self.client.close()
