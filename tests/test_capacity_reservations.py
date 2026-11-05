from datetime import datetime, timedelta, timezone
from threading import Lock
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.storage.azure_session_store import AzureSessionStore
from azure.core.exceptions import ResourceNotFoundError


class FakeEntity(dict):
    pass


class FakeCapacity:
    """Local, thread-safe simulation of Azure Table entities and transactions."""

    def __init__(self, host):
        self.entities = {
            (host["PartitionKey"], host["RowKey"]): host.copy()
        }
        self.lock = Lock()
        self.version = 0

    def get_entity(self, partition_key, row_key):
        with self.lock:
            entity = self.entities.get((partition_key, row_key))
            if entity is None:
                raise ResourceNotFoundError("Entity not found")
            result = FakeEntity(entity)
            result.metadata = {"etag": str(self.version)}
            return result

    def query_entities(self, query):
        with self.lock:
            return [
                entity.copy()
                for (partition, _), entity in self.entities.items()
                if partition == "CAPACITY"
                and entity.get("RowKey", "").startswith("HOST-")
            ]

    def submit_transaction(self, actions):
        with self.lock:
            staged = {
                key: value.copy()
                for key, value in self.entities.items()
            }

            for action, entity, *options in actions:
                key = (entity["PartitionKey"], entity["RowKey"])

                if action == "update":
                    current = staged.get(key)
                    if current is None:
                        raise ResourceNotFoundError("Entity not found")
                    staged[key] = dict(entity)

                elif action == "create":
                    if key in staged:
                        from azure.core.exceptions import ResourceExistsError
                        raise ResourceExistsError("Entity already exists")
                    staged[key] = dict(entity)

                elif action == "delete":
                    if key not in staged:
                        raise ResourceNotFoundError("Entity not found")
                    del staged[key]

                else:
                    raise AssertionError(f"Unexpected action: {action}")

            self.entities = staged
            self.version += 1


def make_store(max_sessions=50, max_memory_mb=300000):
    store = AzureSessionStore.__new__(AzureSessionStore)

    host = {
        "PartitionKey": "CAPACITY",
        "RowKey": "HOST-test-host",
        "HostID": "test-host",
        "Provider": "azure_sandbox",
        "MaxSessions": max_sessions,
        "MaxMemoryMB": max_memory_mb,
        "ReservedSessions": 0,
        "ReservedMemoryMB": 0,
        "Enabled": True,
    }

    store.capacity = FakeCapacity(host)
    store.get_session = lambda session_id: {"id": session_id}

    def creation_lease(session_id, owner_id, token):
        return {
            "PartitionKey": "CAPACITY",
            "RowKey": f"CREATE-{session_id}",
            "OwnerID": owner_id,
            "FencingToken": token,
            "ExpiresAt": (
                datetime.now(timezone.utc) + timedelta(minutes=10)
            ).isoformat(),
        }

    store._test_creation_lease = creation_lease
    return store


def prepare_session(store, session_id, token=1):
    lease = store._test_creation_lease(session_id, "owner", token)
    store.capacity.entities[("CAPACITY", lease["RowKey"])] = lease


def host_entity(store):
    return store.capacity.get_entity("CAPACITY", "HOST-test-host")


def test_reservations_stop_at_session_limit():
    store = make_store(max_sessions=3, max_memory_mb=30000)

    for number in range(3):
        session_id = f"session-{number}"
        prepare_session(store, session_id)
        store.reserve_execution_host(
            session_id, "azure_sandbox", 6144, "owner", 1
        )

    host = host_entity(store)
    assert host["ReservedSessions"] == 3
    assert host["ReservedMemoryMB"] == 18432

    prepare_session(store, "session-over-limit")
    with pytest.raises(RuntimeError, match="No available execution host"):
        store.reserve_execution_host(
            "session-over-limit", "azure_sandbox", 6144, "owner", 1
        )


def test_reservations_stop_at_memory_limit():
    store = make_store(max_sessions=10, max_memory_mb=10000)

    prepare_session(store, "session-1")
    store.reserve_execution_host(
        "session-1", "azure_sandbox", 6144, "owner", 1
    )

    prepare_session(store, "session-2")
    with pytest.raises(RuntimeError, match="No available execution host"):
        store.reserve_execution_host(
            "session-2", "azure_sandbox", 6144, "owner", 1
        )

    host = host_entity(store)
    assert host["ReservedSessions"] == 1
    assert host["ReservedMemoryMB"] == 6144


def test_release_decrements_session_and_memory_reservations():
    store = make_store()
    prepare_session(store, "session-release")

    store.reserve_execution_host(
        "session-release", "azure_sandbox", 6144, "owner", 1
    )

    assert store.release_execution_host("session-release", "test-host", 1)

    host = host_entity(store)
    assert host["ReservedSessions"] == 0
    assert host["ReservedMemoryMB"] == 0


def test_stale_fencing_token_cannot_release_reservation():
    store = make_store()
    prepare_session(store, "session-stale", token=2)

    store.reserve_execution_host(
        "session-stale", "azure_sandbox", 6144, "owner", 2
    )

    assert not store.release_execution_host("session-stale", "test-host", 1)

    host = host_entity(store)
    assert host["ReservedSessions"] == 1
    assert host["ReservedMemoryMB"] == 6144


def test_concurrent_reservations_never_exceed_capacity():
    store = make_store(max_sessions=10, max_memory_mb=100000)

    for number in range(50):
        prepare_session(store, f"concurrent-{number}")

    def reserve(number):
        session_id = f"concurrent-{number}"
        try:
            store.reserve_execution_host(
                session_id, "azure_sandbox", 6144, "owner", 1
            )
            return True
        except RuntimeError:
            return False

    with ThreadPoolExecutor(max_workers=50) as executor:
        results = list(executor.map(reserve, range(50)))

    host = host_entity(store)
    assert sum(results) == 10
    assert host["ReservedSessions"] == 10
    assert host["ReservedMemoryMB"] == 61440
