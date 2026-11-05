import os
import time
from threading import Event, RLock, Thread
from uuid import uuid4

from app.execution.host_registry import host_registry
from app.execution.providers import LocalDockerProvider
from app.storage.session_store_backend import (
    acquire_session_command_lease,
    renew_session_command_lease,
    release_session_command_lease,
    acquire_session_creation_lease,
    release_session_creation_lease,
    renew_session_creation_lease,
    clear_execution_assignment,
    get_session,
    release_execution_host,
    update_execution_assignment,
    update_execution_status,
)


class SessionExecutionAllocator:
    """Assign sessions to persistent execution hosts and providers."""

    def __init__(self):
        # Only protects lazy provider construction, never sandbox startup.
        self._provider_lock = RLock()
        # Do not initialize Docker or Azure clients until a provider is needed.
        self._providers = {}

    def _get_provider(self, provider_name: str):
        with self._provider_lock:
            provider = self._providers.get(provider_name)

            if provider is None and provider_name == "local_docker":
                provider = LocalDockerProvider()
                self._providers[provider_name] = provider

            if provider is None and provider_name == "azure_sandbox":
                from app.execution.azure_sandbox_provider import (
                    AzureSandboxProvider,
                )
                provider = AzureSandboxProvider()
                self._providers[provider_name] = provider

            if provider is None:
                raise RuntimeError(
                    f"Execution provider '{provider_name}' is not configured"
                )

            return provider

    def ensure_session(self, session: dict) -> str:
        """Create or reconnect a session under a cross-replica lease."""
        session_id = session["id"]
        current = get_session(session_id)

        if not current:
            raise ValueError(f"Session not found: {session_id}")

        provider_name = (
            current.get("execution_provider")
            or os.getenv("EXECUTION_PROVIDER", "local_docker")
        )
        provider = self._get_provider(provider_name)

        default_memory = "6144" if provider_name == "azure_sandbox" else "512"
        required_memory_mb = int(
            os.getenv("SESSION_MEMORY_MB", default_memory)
        )
        max_memory_mb = int(os.getenv("SESSION_MAX_MEMORY_MB", "8192"))

        # Azure capacity reservations must match the actual sandbox size.
        # Otherwise the registry could reserve less memory than Azure allocates.
        if provider_name == "azure_sandbox":
            import re

            sandbox_memory = getattr(provider, "memory", "6144Mi")
            match = re.fullmatch(r"(\d+)(Mi|Gi)", sandbox_memory)

            if not match:
                raise ValueError(
                    "AZURE_SANDBOX_MEMORY must use Mi or Gi units"
                )

            sandbox_memory_mb = int(match.group(1)) * (
                1024 if match.group(2) == "Gi" else 1
            )

            if required_memory_mb != sandbox_memory_mb:
                raise ValueError(
                    f"SESSION_MEMORY_MB ({required_memory_mb}) must match "
                    f"AZURE_SANDBOX_MEMORY ({sandbox_memory}) for accurate "
                    "capacity accounting"
                )

        if required_memory_mb < 1 or required_memory_mb > max_memory_mb:
            raise ValueError(
                f"Session memory request ({required_memory_mb} MB) must be "
                f"between 1 MB and the configured per-session limit "
                f"({max_memory_mb} MB)"
            )

        # Acquire the distributed lease BEFORE reserving capacity. This avoids
        # leaking reservations when another backend is already creating it.
        owner_id = str(uuid4())
        deadline = time.monotonic() + 60

        fencing_token = None
        while fencing_token is None:
            fencing_token = acquire_session_creation_lease(
                session_id, owner_id, lease_seconds=900
            )
            if fencing_token is not None:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "Another backend is creating this session's execution "
                    "environment. Retry shortly."
                )
            time.sleep(0.5)

        # Renew the lease while a potentially long sandbox creation runs.
        stop_heartbeat = Event()
        lease_lost = Event()

        def renew_lease():
            while not stop_heartbeat.wait(60):
                try:
                    renewed = renew_session_creation_lease(
                        session_id, owner_id, fencing_token,
                        lease_seconds=900,
                    )
                    if not renewed:
                        lease_lost.set()
                        return
                except Exception:
                    lease_lost.set()
                    return

        heartbeat = Thread(
            target=renew_lease,
            name=f"session-lease-{session_id[:8]}",
            daemon=True,
        )
        heartbeat_started = False
        host = None
        resource_id = None
        allocation_session = current
        try:
            heartbeat.start()
            heartbeat_started = True

            # Always re-read after acquiring the lease. Another request may
            # have completed initialization while this request was waiting.
            current = get_session(session_id)
            if not current:
                raise ValueError(
                    f"Session disappeared during allocation: {session_id}"
                )

            if (
                current.get("execution_status") == "ready"
                and current.get("execution_resource_id")
                and current.get("execution_provider") == provider_name
            ):
                return current["execution_resource_id"]

            host = host_registry.select_host(
                session_id=session_id,
                provider=provider_name,
                required_memory_mb=required_memory_mb,
                owner_id=owner_id,
                fencing_token=fencing_token,
            )

            if not update_execution_assignment(
                session_id=session_id,
                provider=provider_name,
                resource_id="",
                host_id=host.host_id,
                status="starting",
                owner_id=owner_id,
                fencing_token=fencing_token,
            ):
                raise RuntimeError(
                    "Could not persist execution host assignment"
                )

            current = get_session(session_id)
            if not current:
                raise ValueError(
                    f"Session disappeared during allocation: {session_id}"
                )

            allocation_session = current
            resource_id = provider.ensure_session(current)

            if lease_lost.is_set():
                raise RuntimeError(
                    "Lost the session creation lease while starting the "
                    "execution environment. Retry the session."
                )

            if not update_execution_assignment(
                session_id=session_id,
                provider=provider_name,
                resource_id=resource_id,
                host_id=host.host_id,
                status="ready",
                owner_id=owner_id,
                fencing_token=fencing_token,
            ):
                raise RuntimeError(
                    "Could not persist execution resource assignment"
                )

            return resource_id

        except Exception as allocation_error:
            if host is not None:
                if provider_name == "azure_sandbox":
                    try:
                        provider.cleanup_failed_allocation(
                            allocation_session,
                            resource_id,
                            fencing_token,
                        )
                    except Exception as cleanup_error:
                        allocation_error.add_note(
                            f"Sandbox cleanup also failed: {cleanup_error}"
                        )
                try:
                    release_execution_host(
                        session_id, host.host_id, fencing_token
                    )
                finally:
                    clear_execution_assignment(session_id, fencing_token)
            raise
        finally:
            stop_heartbeat.set()
            if heartbeat_started:
                heartbeat.join(timeout=30)
            release_session_creation_lease(session_id, owner_id, fencing_token)

    def _acquire_command_lease(self, session_id: str, lease_seconds: int = 300):
        """Acquire a distributed per-session command lease and renew it."""
        owner_id = str(uuid4())
        deadline = time.monotonic() + 60
        fencing_token = None

        while fencing_token is None:
            fencing_token = acquire_session_command_lease(
                session_id, owner_id, lease_seconds=lease_seconds
            )
            if fencing_token is not None:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "Another operation is running in this session. Retry shortly."
                )
            time.sleep(0.5)

        stop_heartbeat = Event()
        lease_lost = Event()

        def heartbeat():
            while not stop_heartbeat.wait(60):
                try:
                    renewed = renew_session_command_lease(
                        session_id, owner_id, fencing_token,
                        lease_seconds=lease_seconds,
                    )
                    if not renewed:
                        lease_lost.set()
                        return
                except Exception:
                    lease_lost.set()
                    return

        thread = Thread(
            target=heartbeat,
            name=f"command-lease-{session_id}",
            daemon=True,
        )
        try:
            thread.start()
        except Exception:
            release_session_command_lease(
                session_id, owner_id, fencing_token
            )
            raise

        return owner_id, fencing_token, stop_heartbeat, lease_lost, thread

    def _release_command_lease(
        self, session_id, owner_id, fencing_token, stop_heartbeat, thread
    ):
        stop_heartbeat.set()
        thread.join(timeout=30)
        release_session_command_lease(
            session_id, owner_id, fencing_token
        )

    def run_command(
        self,
        session: dict,
        command: str,
        timeout: int = 30,
    ) -> dict:
        """Serialize initialization and command execution under a session lease."""
        session_id = session["id"]

        # Acquire before initialization so stop/release cannot race with startup.
        owner_id, token, stop_heartbeat, lease_lost, thread = (
            self._acquire_command_lease(session_id)
        )
        try:
            current = get_session(session_id)
            if not current:
                raise ValueError(f"Session not found: {session_id}")

            if (
                not current.get("execution_provider")
                or current.get("execution_status") != "ready"
            ):
                self.ensure_session(current)
                current = get_session(session_id)

            if not current or current.get("execution_status") != "ready":
                raise RuntimeError("Session execution environment is not ready")

            if lease_lost.is_set():
                raise RuntimeError(
                    "Command lease was lost before execution; retry the command."
                )

            provider = self._get_provider(current["execution_provider"])
            result = provider.run_command(
                session=current,
                command=command,
                timeout=timeout,
            )

            if lease_lost.is_set():
                raise RuntimeError(
                    "Command lease was lost during execution; "
                    "the result cannot be treated as authoritative."
                )
            return result
        finally:
            self._release_command_lease(
                session_id, owner_id, token, stop_heartbeat, thread
            )

    def stop_session(self, session: dict) -> None:
        """Stop execution under the per-session command lease."""
        session_id = session["id"]
        current = get_session(session_id)
        if not current or not current.get("execution_provider"):
            return

        owner_id, token, stop_heartbeat, lease_lost, thread = (
            self._acquire_command_lease(session_id, lease_seconds=900)
        )
        try:
            current = get_session(session_id)
            if not current or not current.get("execution_provider"):
                return
            provider = self._get_provider(current["execution_provider"])
            provider.stop_session(session_id)
            if lease_lost.is_set():
                raise RuntimeError("Command lease was lost while stopping the session")
            update_execution_status(
                session_id, "stopped", current.get("execution_fencing_token")
            )
        except Exception:
            update_execution_status(
                session_id, "error", current.get("execution_fencing_token")
            )
            raise
        finally:
            self._release_command_lease(
                session_id, owner_id, token, stop_heartbeat, thread
            )

    def release_session(self, session_id: str) -> None:
        """Stop execution and release capacity under the per-session lease."""
        current = get_session(session_id)
        if not current:
            return

        owner_id, token, stop_heartbeat, lease_lost, thread = (
            self._acquire_command_lease(session_id, lease_seconds=900)
        )
        try:
            current = get_session(session_id)
            if not current:
                return

            provider_name = current.get("execution_provider")
            host_id = current.get("execution_host_id")
            if provider_name:
                provider = self._get_provider(provider_name)
                provider.stop_session(session_id)

            if lease_lost.is_set():
                raise RuntimeError("Command lease was lost while releasing the session")

            if host_id:
                release_execution_host(
                    session_id, host_id,
                    current.get("execution_fencing_token"),
                )

            clear_execution_assignment(
                session_id, current.get("execution_fencing_token")
            )
        finally:
            self._release_command_lease(
                session_id, owner_id, token, stop_heartbeat, thread
            )

    def close(self) -> None:
        with self._provider_lock:
            providers = list(self._providers.values())

        for provider in providers:
            provider.close()


