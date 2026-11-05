
import os
from threading import Lock

_store = None
_local_lease_guard = Lock()
_local_leases = {}
_local_lease_tokens = {}
_local_command_leases = {}
_local_command_lease_tokens = {}


def initialize_session_store():
    """Initialize the configured session metadata backend."""
    global _store

    backend = os.getenv("SESSION_STORE_BACKEND", "local").lower()

    if backend == "local":
        from app.storage.session_store import initialize_database
        initialize_database()
        _store = None
        return

    if backend == "azure_tables":
        from app.storage.azure_session_store import AzureSessionStore
        _store = AzureSessionStore()
        return

    raise ValueError(f"Unsupported session store backend: {backend}")


def _get_store():
    global _store

    backend = os.getenv("SESSION_STORE_BACKEND", "local").lower()

    if backend == "local":
        return None

    if backend == "azure_tables":
        if _store is None:
            from app.storage.azure_session_store import AzureSessionStore
            _store = AzureSessionStore()
        return _store

    raise ValueError(f"Unsupported session store backend: {backend}")


def create_session(title="New chat", execution_provider=None):
    store = _get_store()
    if store:
        return store.create_session(title, execution_provider)

    from app.storage.session_store import create_session as local_create
    return local_create(title, execution_provider)


def get_session(session_id):
    store = _get_store()
    if store:
        return store.get_session(session_id)

    from app.storage.session_store import get_session as local_get
    return local_get(session_id)


def list_sessions():
    store = _get_store()
    if store:
        return store.list_sessions()

    from app.storage.session_store import list_sessions as local_list
    return local_list()


def update_claude_session_id(session_id, claude_session_id):
    store = _get_store()
    if store:
        return store.update_claude_session_id(
            session_id, claude_session_id
        )

    from app.storage.session_store import (
        update_claude_session_id as local_update,
    )
    return local_update(session_id, claude_session_id)


def update_execution_assignment(
    session_id, provider, resource_id, status="ready", host_id=None,
    owner_id=None, fencing_token=None,
):
    store = _get_store()
    if store:
        return store.update_execution_assignment(
            session_id, provider, resource_id, status, host_id,
            owner_id, fencing_token,
        )
    from app.storage.session_store import update_execution_assignment as local_update
    return local_update(session_id, provider, resource_id, status, host_id)


def update_execution_status(session_id, status, fencing_token=None):
    store = _get_store()
    if store:
        return store.update_execution_status(session_id, status, fencing_token)
    from app.storage.session_store import update_execution_status as local_update
    return local_update(session_id, status)


def clear_execution_assignment(session_id, fencing_token=None):
    store = _get_store()
    if store:
        return store.clear_execution_assignment(session_id, fencing_token)
    from app.storage.session_store import clear_execution_assignment as local_clear
    return local_clear(session_id)


def register_execution_host(
    host_id, provider, max_sessions, max_memory_mb, enabled=True
):
    store = _get_store()
    if store:
        return store.register_execution_host(
            host_id, provider, max_sessions, max_memory_mb, enabled
        )

    from app.storage.session_store import (
        register_execution_host as local_register,
    )
    return local_register(
        host_id, provider, max_sessions, max_memory_mb, enabled
    )


def list_execution_hosts():
    store = _get_store()
    if store:
        return store.list_execution_hosts()

    from app.storage.session_store import (
        list_execution_hosts as local_list,
    )
    return local_list()


def reserve_execution_host(session_id, provider, required_memory_mb, owner_id=None, fencing_token=None):
    store = _get_store()
    if store:
        return store.reserve_execution_host(
            session_id, provider, required_memory_mb, owner_id, fencing_token
        )

    from app.storage.session_store import (
        reserve_execution_host as local_reserve,
    )
    return local_reserve(session_id, provider, required_memory_mb)



def release_execution_host(session_id, host_id, fencing_token=None):
    store = _get_store()
    if store:
        return store.release_execution_host(session_id, host_id, fencing_token)
    # Local capacity is derived from session assignments.
    return False


def acquire_session_creation_lease(
    session_id, owner_id, lease_seconds=900
):
    store = _get_store()
    if store:
        return store.acquire_session_creation_lease(
            session_id, owner_id, lease_seconds
        )

    with _local_lease_guard:
        if session_id in _local_leases:
            return None
        token = _local_lease_tokens.get(session_id, 0) + 1
        _local_lease_tokens[session_id] = token
        _local_leases[session_id] = {
            "owner": owner_id,
            "token": token,
        }
        return token


def renew_session_creation_lease(
    session_id, owner_id, fencing_token, lease_seconds=900
):
    store = _get_store()
    if store:
        return store.renew_session_creation_lease(
            session_id, owner_id, fencing_token, lease_seconds
        )

    with _local_lease_guard:
        current = _local_leases.get(session_id)
        return bool(
            current
            and current["owner"] == owner_id
            and current["token"] == fencing_token
        )


def release_session_creation_lease(
    session_id, owner_id, fencing_token
):
    store = _get_store()
    if store:
        return store.release_session_creation_lease(
            session_id, owner_id, fencing_token
        )

    with _local_lease_guard:
        current = _local_leases.get(session_id)
        if (
            current
            and current["owner"] == owner_id
            and current["token"] == fencing_token
        ):
            del _local_leases[session_id]
            return True
        return False


def close_session_store():
    global _store

    if _store is not None:
        _store.close()
        _store = None



def acquire_session_command_lease(session_id, owner_id, lease_seconds=300):
    store = _get_store()
    if store:
        return store.acquire_session_command_lease(
            session_id, owner_id, lease_seconds
        )

    import time
    with _local_lease_guard:
        now = time.monotonic()
        current = _local_command_leases.get(session_id)
        if current and current["expires_at"] > now:
            return None

        token = _local_command_lease_tokens.get(session_id, 0) + 1
        _local_command_lease_tokens[session_id] = token
        _local_command_leases[session_id] = {
            "owner": owner_id,
            "token": token,
            "expires_at": now + lease_seconds,
        }
        return token


def renew_session_command_lease(
    session_id, owner_id, fencing_token, lease_seconds=300
):
    store = _get_store()
    if store:
        return store.renew_session_command_lease(
            session_id, owner_id, fencing_token, lease_seconds
        )

    import time
    with _local_lease_guard:
        current = _local_command_leases.get(session_id)
        if not current:
            return False
        if (
            current["owner"] != owner_id
            or current["token"] != fencing_token
            or current["expires_at"] <= time.monotonic()
        ):
            return False
        current["expires_at"] = time.monotonic() + lease_seconds
        return True


def release_session_command_lease(session_id, owner_id, fencing_token):
    store = _get_store()
    if store:
        return store.release_session_command_lease(
            session_id, owner_id, fencing_token
        )

    with _local_lease_guard:
        current = _local_command_leases.get(session_id)
        if (
            current
            and current["owner"] == owner_id
            and current["token"] == fencing_token
        ):
            del _local_command_leases[session_id]
            return True
        return False

