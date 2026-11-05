from typing import Any, Protocol

from app.execution.docker_executor import DockerExecutor


class ExecutionProvider(Protocol):
    """Common interface for local and cloud execution providers."""

    name: str

    def ensure_session(self, session: dict) -> str:
        """Start or reconnect to a session's execution environment."""
        ...

    def run_command(
        self,
        session: dict,
        command: str,
        timeout: int = 30,
    ) -> dict:
        ...

    def stop_session(self, session_id: str) -> None:
        ...

    def close(self) -> None:
        ...


class LocalDockerProvider:
    """Local development provider backed by Docker."""

    name = "local_docker"

    def __init__(self):
        self.executor = DockerExecutor()

    def ensure_session(self, session: dict) -> str:
        container = self.executor.get_or_create_container(session)
        return container.id

    def run_command(self, session, command, timeout=30):
        return self.executor.run_command(
            session=session,
            command=command,
            timeout_seconds=timeout,
        )

    def stop_session(self, session_id: str) -> None:
        self.executor.stop_container(session_id)

    def close(self) -> None:
        self.executor.close()
