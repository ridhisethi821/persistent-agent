import os
from dataclasses import dataclass

from app.storage.session_store_backend import (
    list_execution_hosts,
    register_execution_host,
    reserve_execution_host,
)


@dataclass
class ExecutionHost:
    host_id: str
    provider: str
    max_sessions: int
    max_memory_mb: int
    active_sessions: int = 0
    enabled: bool = True


class ExecutionHostRegistry:
    """Persistent registry of local and Azure execution hosts."""

    def __init__(self):
        self._default_host_registered = False

    def register(self, host: ExecutionHost) -> None:
        register_execution_host(
            host_id=host.host_id,
            provider=host.provider,
            max_sessions=host.max_sessions,
            max_memory_mb=host.max_memory_mb,
            enabled=host.enabled,
        )

    def ensure_default_host(self) -> None:
        if self._default_host_registered:
            return

        default_provider = os.getenv(
            "EXECUTION_PROVIDER", "local_docker"
        ).lower()

        if default_provider == "local_docker":
            self.register(
                ExecutionHost(
                    host_id="local-docker-host",
                    provider="local_docker",
                    max_sessions=int(os.getenv("LOCAL_MAX_SESSIONS", "10")),
                    max_memory_mb=int(
                        os.getenv("LOCAL_HOST_MEMORY_MB", "4096")
                    ),
                )
            )

        groups = []
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
            else:
                group_name = item

            groups.append(group_name)

        if groups:
            total_sessions = int(
                os.getenv("SESSION_MAX_CONCURRENT", "50")
            )
            memory_per_session = int(
                os.getenv("SESSION_MEMORY_MB", "6144")
            )

            base_capacity, remainder = divmod(
                total_sessions, len(groups)
            )

            for index, group_name in enumerate(groups):
                group_capacity = base_capacity + (
                    1 if index < remainder else 0
                )

                if group_capacity < 1:
                    continue

                self.register(
                    ExecutionHost(
                        host_id=group_name,
                        provider="azure_sandbox",
                        max_sessions=group_capacity,
                        max_memory_mb=(
                            group_capacity * memory_per_session
                        ),
                    )
                )

        self._default_host_registered = True

    def select_host(
        self,
        session_id: str,
        provider: str,
        required_memory_mb: int,
        owner_id: str,
        fencing_token: int,
    ) -> ExecutionHost:
        self.ensure_default_host()

        host = reserve_execution_host(
            session_id=session_id,
            provider=provider,
            required_memory_mb=required_memory_mb,
            owner_id=owner_id,
            fencing_token=fencing_token,
        )

        return ExecutionHost(
            host_id=host["host_id"],
            provider=host["provider"],
            max_sessions=host["max_sessions"],
            max_memory_mb=host["max_memory_mb"],
            active_sessions=host["active_sessions"],
            enabled=bool(host["enabled"]),
        )

    def release_host(self, host_id: str) -> None:
        return None

    def list_hosts(self) -> list[dict]:
        self.ensure_default_host()
        return list_execution_hosts()


host_registry = ExecutionHostRegistry()
