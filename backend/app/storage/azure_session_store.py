
import os
from uuid import uuid4
from datetime import datetime, timezone, timedelta

from azure.core import MatchConditions
from azure.core.exceptions import (
    ResourceNotFoundError,
    ResourceExistsError,
    ResourceModifiedError,
)
from azure.data.tables import TableServiceClient


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class AzureSessionStore:
    """Shared session metadata and execution capacity using Azure Tables."""

    def __init__(self):
        endpoint = os.getenv("AZURE_TABLES_ENDPOINT")
        if not endpoint:
            raise RuntimeError("AZURE_TABLES_ENDPOINT is required")

        self.credential = None
        from azure.identity import DefaultAzureCredential

        self.credential = DefaultAzureCredential()
        self.service = TableServiceClient(
            endpoint=endpoint,
            credential=self.credential,
            connection_timeout=10,
            read_timeout=15,
            retry_total=1,
            retry_backoff_max=1,
        )

        self.sessions = self.service.create_table_if_not_exists(
            "AgentSessions"
        )
        self.capacity = self.service.create_table_if_not_exists(
            "ExecutionCapacity"
        )

    def close(self):
        self.service.close()
        self.credential.close()

    # Session operations

    def create_session(self, title="New chat", execution_provider=None):
        session_id = str(uuid4())
        now = utc_now()

        session = {
            "id": session_id,
            "title": title,
            "status": "active",
            "workspace_relpath": f"workspaces/{session_id}",
            "created_at": now,
            "updated_at": now,
            "claude_session_id": "",
            "execution_provider": execution_provider or "",
            "execution_resource_id": "",
            "execution_host_id": "",
            "execution_status": "unassigned",
        }

        entity = {
            "PartitionKey": "SESSIONS",
            "RowKey": session_id,
            **session,
        }
        self.sessions.create_entity(entity)
        return session

    def get_session(self, session_id):
        try:
            entity = self.sessions.get_entity(
                partition_key="SESSIONS",
                row_key=session_id,
            )
            session = self._session_dict(entity)
            assignment = self._get_execution_assignment(session_id)
            if assignment:
                session.update(assignment)
            return session
        except ResourceNotFoundError:
            return None

    def list_sessions(self):
        entities = self.sessions.query_entities(
            "PartitionKey eq 'SESSIONS'"
        )
        sessions = [self._session_dict(item) for item in entities]
        for session in sessions:
            assignment = self._get_execution_assignment(session["id"])
            if assignment:
                session.update(assignment)
        return sorted(
            sessions,
            key=lambda item: item["updated_at"],
            reverse=True,
        )

    def update_claude_session_id(self, session_id, claude_session_id):
        return self._update_session(
            session_id,
            {
                "claude_session_id": claude_session_id,
                "updated_at": utc_now(),
            },
        )

    def update_execution_assignment(
        self, session_id, provider, resource_id,
        status="ready", host_id=None, owner_id=None, fencing_token=None,
    ):
        if owner_id is None or fencing_token is None:
            raise ValueError("Azure assignment requires lease ownership")
        return self.commit_execution_assignment(
            session_id, owner_id, fencing_token,
            provider, resource_id, status, host_id,
        )


    def update_execution_status(self, session_id, status, fencing_token=None):
        key = self._assignment_row_key(session_id)
        try:
            entity = self.capacity.get_entity(self.CAPACITY_PARTITION, key)
            if fencing_token is not None and int(entity.get("FencingToken", 0)) != fencing_token:
                return False
            entity["Status"] = status
            entity["UpdatedAt"] = utc_now()
            self.capacity.update_entity(
                entity, mode="merge",
                etag=entity.metadata["etag"],
                match_condition=MatchConditions.IfNotModified,
            )
            return True
        except (ResourceNotFoundError, ResourceModifiedError):
            return False


    def clear_execution_assignment(self, session_id, fencing_token=None):
        key = self._assignment_row_key(session_id)
        try:
            entity = self.capacity.get_entity(self.CAPACITY_PARTITION, key)
            if fencing_token is not None and int(entity.get("FencingToken", 0)) != fencing_token:
                return False
            entity.update({
                "Provider": "", "ResourceID": "", "HostID": "",
                "Status": "unassigned", "UpdatedAt": utc_now(),
            })
            self.capacity.update_entity(
                entity, mode="merge",
                etag=entity.metadata["etag"],
                match_condition=MatchConditions.IfNotModified,
            )
            return True
        except (ResourceNotFoundError, ResourceModifiedError):
            return False


    def _update_session(self, session_id, changes):
        for attempt in range(3):
            try:
                entity = self.sessions.get_entity(
                    "SESSIONS",
                    session_id,
                )

                entity.update(changes)

                self.sessions.update_entity(
                    entity,
                    mode="merge",
                    etag=entity.metadata["etag"],
                    match_condition=MatchConditions.IfNotModified,
                )
                return True

            except ResourceNotFoundError:
                return False

            except ResourceModifiedError:
                if attempt == 2:
                    raise

        return False

    @staticmethod
    def _session_dict(entity):
        fields = [
            "id",
            "title",
            "status",
            "workspace_relpath",
            "created_at",
            "updated_at",
            "claude_session_id",
            "execution_provider",
            "execution_resource_id",
            "execution_host_id",
            "execution_status",
        ]
        result = {field: entity.get(field) for field in fields}
        for field in (
            "claude_session_id",
            "execution_provider",
            "execution_resource_id",
            "execution_host_id",
        ):
            if result[field] == "":
                result[field] = None
        return result

    
    @staticmethod
    def _assignment_row_key(session_id):
        return f"ASSIGN-{session_id}"

    def _get_execution_assignment(self, session_id):
        try:
            entity = self.capacity.get_entity(
                self.CAPACITY_PARTITION, self._assignment_row_key(session_id)
            )
            return {
                "execution_provider": entity.get("Provider") or None,
                "execution_resource_id": entity.get("ResourceID") or None,
                "execution_host_id": entity.get("HostID") or None,
                "execution_status": entity.get("Status", "unassigned"),
                "execution_fencing_token": int(entity.get("FencingToken", 0)),
            }
        except ResourceNotFoundError:
            return None

    def commit_execution_assignment(
        self, session_id, owner_id, fencing_token,
        provider, resource_id, status, host_id,
    ):
        lease_key = self._creation_lease_row_key(session_id)
        assignment_key = self._assignment_row_key(session_id)

        for attempt in range(5):
            try:
                lease = self.capacity.get_entity(
                    self.CAPACITY_PARTITION, lease_key
                )
                expiry = datetime.fromisoformat(lease["ExpiresAt"])
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
                if (
                    lease.get("OwnerID") != owner_id
                    or int(lease.get("FencingToken", 0)) != fencing_token
                    or expiry.timestamp()
                    <= datetime.now(timezone.utc).timestamp() + 30
                ):
                    return False

                lease["LastAssignmentCommitAt"] = utc_now()
                assignment = {
                    "PartitionKey": self.CAPACITY_PARTITION,
                    "RowKey": assignment_key,
                    "SessionID": session_id,
                    "Provider": provider or "",
                    "ResourceID": resource_id or "",
                    "HostID": host_id or "",
                    "Status": status,
                    "FencingToken": fencing_token,
                    "UpdatedAt": utc_now(),
                }
                try:
                    self.capacity.submit_transaction([
                        ("update", lease, {
                            "etag": lease.metadata["etag"],
                            "match_condition": MatchConditions.IfNotModified,
                        }),
                        ("upsert", assignment, {"mode": "replace"}),
                    ])
                    return True
                except ResourceModifiedError:
                    if attempt == 4:
                        return False
            except ResourceNotFoundError:
                return False
        return False

    # Execution host registration and atomic allocation

    CAPACITY_PARTITION = "CAPACITY"

    @staticmethod
    def _host_row_key(host_id):
        return f"HOST-{host_id}"

    @staticmethod
    def _reservation_row_key(session_id):
        return f"SESSION-{session_id}"

    @staticmethod
    def _host_dict(entity):
        return {
            "host_id": entity["HostID"],
            "provider": entity["Provider"],
            "max_sessions": entity["MaxSessions"],
            "max_memory_mb": entity["MaxMemoryMB"],
            "active_sessions": entity.get("ReservedSessions", 0),
            "reserved_memory_mb": entity.get("ReservedMemoryMB", 0),
            "enabled": entity["Enabled"],
        }

    def register_execution_host(
        self,
        host_id,
        provider,
        max_sessions,
        max_memory_mb,
        enabled=True,
    ):
        """Create or update host configuration without resetting reservations."""
        if max_sessions < 1 or max_memory_mb < 1:
            raise ValueError("Host capacity values must be positive")

        row_key = self._host_row_key(host_id)
        config = {
            "PartitionKey": self.CAPACITY_PARTITION,
            "RowKey": row_key,
            "HostID": host_id,
            "Provider": provider,
            "MaxSessions": max_sessions,
            "MaxMemoryMB": max_memory_mb,
            "Enabled": enabled,
        }

        for attempt in range(5):
            try:
                current = self.capacity.get_entity(
                    self.CAPACITY_PARTITION, row_key
                )
            except ResourceNotFoundError:
                try:
                    self.capacity.create_entity(config)
                    return
                except ResourceExistsError:
                    continue

            reserved_sessions = current.get("ReservedSessions", 0)
            reserved_memory = current.get("ReservedMemoryMB", 0)

            if max_sessions < reserved_sessions:
                raise ValueError(
                    f"Cannot reduce host '{host_id}' capacity below its "
                    f"{reserved_sessions} currently reserved sessions"
                )
            if max_memory_mb < reserved_memory:
                raise ValueError(
                    f"Cannot reduce host '{host_id}' memory below its "
                    f"{reserved_memory} MB currently reserved"
                )

            current.update(config)
            try:
                self.capacity.update_entity(
                    current,
                    mode="merge",
                    etag=current.metadata["etag"],
                    match_condition=MatchConditions.IfNotModified,
                )
                return
            except ResourceModifiedError:
                # Another backend changed the host or its reservations.
                continue

        raise RuntimeError(
            f"Could not register execution host '{host_id}' after retries"
        )

    def list_execution_hosts(self):
        entities = self.capacity.query_entities(
            "PartitionKey eq 'CAPACITY' "
            "and RowKey ge 'HOST-' "
            "and RowKey lt 'HOST.'"
        )

        return [
            self._host_dict(entity)
            for entity in entities
        ]

    def _get_existing_reservation(self, session_id):
        try:
            return self.capacity.get_entity(
                self.CAPACITY_PARTITION,
                self._reservation_row_key(session_id),
            )
        except ResourceNotFoundError:
            return None

    def reserve_execution_host(
        self,
        session_id,
        provider,
        required_memory_mb,
        owner_id,
        fencing_token,
    ):
        """Reserve capacity only while the caller owns the current lease."""
        if required_memory_mb < 1:
            raise ValueError("Required memory must be positive")
        if not owner_id or fencing_token is None or int(fencing_token) < 1:
            raise ValueError("A valid lease owner and fencing token are required")

        fencing_token = int(fencing_token)
        session = self.get_session(session_id)
        if not session:
            raise ValueError(f"Session not found: {session_id}")

        lease_key = self._creation_lease_row_key(session_id)
        reservation_key = self._reservation_row_key(session_id)

        def get_valid_lease():
            lease = self.capacity.get_entity(
                self.CAPACITY_PARTITION, lease_key
            )
            expiry = datetime.fromisoformat(lease["ExpiresAt"])
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if (
                lease.get("OwnerID") != owner_id
                or int(lease.get("FencingToken", 0)) != fencing_token
                or expiry <= datetime.now(timezone.utc)
            ):
                raise RuntimeError("Session creation lease is no longer owned")
            return lease

        for attempt in range(5):
            try:
                lease = get_valid_lease()
                try:
                    reservation = self.capacity.get_entity(
                        self.CAPACITY_PARTITION, reservation_key
                    )
                except ResourceNotFoundError:
                    reservation = None

                if reservation is not None:
                    if reservation["Provider"] != provider:
                        raise RuntimeError(
                            "Existing reservation uses a different provider"
                        )
                    if int(reservation.get("MemoryMB", 0)) < required_memory_mb:
                        raise RuntimeError(
                            "Existing reservation has insufficient memory"
                        )
                    if int(reservation.get("FencingToken", 0)) > fencing_token:
                        raise RuntimeError(
                            "A newer allocation already owns this reservation"
                        )

                    host = self.capacity.get_entity(
                        self.CAPACITY_PARTITION,
                        self._host_row_key(reservation["HostID"]),
                    )
                    if not host["Enabled"] or host["Provider"] != provider:
                        raise RuntimeError("Existing execution host is unavailable")

                    reservation["FencingToken"] = fencing_token
                    self.capacity.submit_transaction([
                        (
                            "update",
                            lease,
                            {
                                "etag": lease.metadata["etag"],
                                "match_condition": MatchConditions.IfNotModified,
                            },
                        ),
                        (
                            "update",
                            reservation,
                            {
                                "etag": reservation.metadata["etag"],
                                "match_condition": MatchConditions.IfNotModified,
                            },
                        ),
                    ])
                    return self._host_dict(host)

                for candidate in self.list_execution_hosts():
                    if not candidate["enabled"] or candidate["provider"] != provider:
                        continue
                    if candidate["active_sessions"] >= candidate["max_sessions"]:
                        continue
                    if (
                        candidate["reserved_memory_mb"] + required_memory_mb
                        > candidate["max_memory_mb"]
                    ):
                        continue

                    host = self.capacity.get_entity(
                        self.CAPACITY_PARTITION,
                        self._host_row_key(candidate["host_id"]),
                    )
                    if not host["Enabled"] or host["Provider"] != provider:
                        continue
                    if host.get("ReservedSessions", 0) >= host["MaxSessions"]:
                        continue
                    if (
                        host.get("ReservedMemoryMB", 0) + required_memory_mb
                        > host["MaxMemoryMB"]
                    ):
                        continue

                    host["ReservedSessions"] = (
                        host.get("ReservedSessions", 0) + 1
                    )
                    host["ReservedMemoryMB"] = (
                        host.get("ReservedMemoryMB", 0) + required_memory_mb
                    )
                    reservation = {
                        "PartitionKey": self.CAPACITY_PARTITION,
                        "RowKey": reservation_key,
                        "SessionID": session_id,
                        "HostID": candidate["host_id"],
                        "Provider": provider,
                        "MemoryMB": required_memory_mb,
                        "FencingToken": fencing_token,
                    }

                    self.capacity.submit_transaction([
                        (
                            "update",
                            lease,
                            {
                                "etag": lease.metadata["etag"],
                                "match_condition": MatchConditions.IfNotModified,
                            },
                        ),
                        (
                            "update",
                            host,
                            {
                                "etag": host.metadata["etag"],
                                "match_condition": MatchConditions.IfNotModified,
                            },
                        ),
                        ("create", reservation),
                    ])
                    return self._host_dict(host)

                raise RuntimeError(
                    f"No available execution host for provider '{provider}'"
                )

            except ResourceModifiedError:
                if attempt == 4:
                    raise RuntimeError(
                        "Reservation changed concurrently; retry allocation"
                    )
            except ResourceExistsError:
                if attempt == 4:
                    raise RuntimeError(
                        "Another allocator created this reservation; retry"
                    )
            except ResourceNotFoundError:
                if attempt == 4:
                    raise RuntimeError(
                        "Lease, reservation, or host disappeared during allocation"
                    )

        raise RuntimeError("Could not reserve execution capacity")


    @staticmethod
    def _creation_lease_row_key(session_id):
        return f"CREATE-{session_id}"

    def acquire_session_creation_lease(
        self, session_id, owner_id, lease_seconds=900
    ):
        """Acquire an expiring lease and return its fencing token."""
        now = datetime.now(timezone.utc)
        expires_at = (now + timedelta(seconds=lease_seconds)).isoformat()
        row_key = self._creation_lease_row_key(session_id)

        for attempt in range(5):
            try:
                current = self.capacity.get_entity(
                    self.CAPACITY_PARTITION, row_key
                )
            except ResourceNotFoundError:
                entity = {
                    "PartitionKey": self.CAPACITY_PARTITION,
                    "RowKey": row_key,
                    "SessionID": session_id,
                    "OwnerID": owner_id,
                    "ExpiresAt": expires_at,
                    "FencingToken": 1,
                }
                try:
                    self.capacity.create_entity(entity)
                    return 1
                except ResourceExistsError:
                    continue

            expiry = datetime.fromisoformat(current["ExpiresAt"])
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)

            current_owner = current.get("OwnerID", "")
            current_token = int(current.get("FencingToken", 0))

            if current_owner and expiry > now:
                return None

            new_token = current_token + 1
            current["OwnerID"] = owner_id
            current["ExpiresAt"] = expires_at
            current["FencingToken"] = new_token

            try:
                self.capacity.update_entity(
                    current,
                    mode="merge",
                    etag=current.metadata["etag"],
                    match_condition=MatchConditions.IfNotModified,
                )
                return new_token
            except ResourceModifiedError:
                if attempt == 4:
                    return None

        return None

    def renew_session_creation_lease(
        self, session_id, owner_id, fencing_token, lease_seconds=900
    ):
        """Renew only when the owner and fencing token still match."""
        row_key = self._creation_lease_row_key(session_id)

        try:
            lease = self.capacity.get_entity(
                self.CAPACITY_PARTITION, row_key
            )
            expiry = datetime.fromisoformat(lease["ExpiresAt"])
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)

            if (
                lease.get("OwnerID") != owner_id
                or int(lease.get("FencingToken", 0)) != fencing_token
                or expiry <= datetime.now(timezone.utc)
            ):
                return False

            lease["ExpiresAt"] = (
                datetime.now(timezone.utc)
                + timedelta(seconds=lease_seconds)
            ).isoformat()

            self.capacity.update_entity(
                lease,
                mode="merge",
                etag=lease.metadata["etag"],
                match_condition=MatchConditions.IfNotModified,
            )
            return True
        except (ResourceNotFoundError, ResourceModifiedError):
            return False


    def release_session_creation_lease(
        self, session_id, owner_id, fencing_token
    ):
        """Release ownership while retaining the fencing counter."""
        row_key = self._creation_lease_row_key(session_id)

        try:
            lease = self.capacity.get_entity(
                self.CAPACITY_PARTITION, row_key
            )
            if (
                lease.get("OwnerID") != owner_id
                or int(lease.get("FencingToken", 0)) != fencing_token
            ):
                return False

            lease["OwnerID"] = ""
            lease["ExpiresAt"] = datetime.now(timezone.utc).isoformat()

            self.capacity.update_entity(
                lease,
                mode="merge",
                etag=lease.metadata["etag"],
                match_condition=MatchConditions.IfNotModified,
            )
            return True
        except (ResourceNotFoundError, ResourceModifiedError):
            return False



    @staticmethod
    def _command_lease_row_key(session_id):
        return f"COMMAND-{session_id}"

    def acquire_session_command_lease(
        self, session_id, owner_id, lease_seconds=300
    ):
        """Acquire an expiring command lease with a monotonic fencing token."""
        now = datetime.now(timezone.utc)
        expires_at = (now + timedelta(seconds=lease_seconds)).isoformat()
        row_key = self._command_lease_row_key(session_id)

        for attempt in range(5):
            try:
                current = self.capacity.get_entity(
                    self.CAPACITY_PARTITION, row_key
                )
            except ResourceNotFoundError:
                entity = {
                    "PartitionKey": self.CAPACITY_PARTITION,
                    "RowKey": row_key,
                    "SessionID": session_id,
                    "OwnerID": owner_id,
                    "ExpiresAt": expires_at,
                    "FencingToken": 1,
                }
                try:
                    self.capacity.create_entity(entity)
                    return 1
                except ResourceExistsError:
                    continue

            expiry = datetime.fromisoformat(current["ExpiresAt"])
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)

            if current.get("OwnerID") and expiry > now:
                return None

            token = int(current.get("FencingToken", 0)) + 1
            current["OwnerID"] = owner_id
            current["ExpiresAt"] = expires_at
            current["FencingToken"] = token

            try:
                self.capacity.update_entity(
                    current,
                    mode="merge",
                    etag=current.metadata["etag"],
                    match_condition=MatchConditions.IfNotModified,
                )
                return token
            except ResourceModifiedError:
                if attempt == 4:
                    return None

        return None

    def renew_session_command_lease(
        self, session_id, owner_id, fencing_token, lease_seconds=300
    ):
        """Renew a command lease only while ownership and token remain valid."""
        row_key = self._command_lease_row_key(session_id)

        try:
            lease = self.capacity.get_entity(
                self.CAPACITY_PARTITION, row_key
            )
            expiry = datetime.fromisoformat(lease["ExpiresAt"])
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)

            if (
                lease.get("OwnerID") != owner_id
                or int(lease.get("FencingToken", 0)) != fencing_token
                or expiry <= datetime.now(timezone.utc)
            ):
                return False

            lease["ExpiresAt"] = (
                datetime.now(timezone.utc)
                + timedelta(seconds=lease_seconds)
            ).isoformat()

            self.capacity.update_entity(
                lease,
                mode="merge",
                etag=lease.metadata["etag"],
                match_condition=MatchConditions.IfNotModified,
            )
            return True
        except (ResourceNotFoundError, ResourceModifiedError):
            return False

    def release_session_command_lease(
        self, session_id, owner_id, fencing_token
    ):
        """Release command ownership without resetting its fencing counter."""
        row_key = self._command_lease_row_key(session_id)

        try:
            lease = self.capacity.get_entity(
                self.CAPACITY_PARTITION, row_key
            )
            if (
                lease.get("OwnerID") != owner_id
                or int(lease.get("FencingToken", 0)) != fencing_token
            ):
                return False

            lease["OwnerID"] = ""
            lease["ExpiresAt"] = datetime.now(timezone.utc).isoformat()

            self.capacity.update_entity(
                lease,
                mode="merge",
                etag=lease.metadata["etag"],
                match_condition=MatchConditions.IfNotModified,
            )
            return True
        except (ResourceNotFoundError, ResourceModifiedError):
            return False

    def release_execution_host(self, session_id, host_id, fencing_token):
        """Release capacity only when the reservation token still matches."""
        if fencing_token is None:
            raise ValueError("A fencing token is required to release capacity")

        fencing_token = int(fencing_token)
        for attempt in range(5):
            try:
                reservation = self._get_existing_reservation(session_id)
                if not reservation:
                    return False
                if reservation["HostID"] != host_id:
                    raise ValueError("Reservation belongs to a different host")
                if int(reservation.get("FencingToken", 0)) != fencing_token:
                    return False

                host = self.capacity.get_entity(
                    self.CAPACITY_PARTITION,
                    self._host_row_key(host_id),
                )
                host["ReservedSessions"] = max(
                    0, host.get("ReservedSessions", 0) - 1
                )
                host["ReservedMemoryMB"] = max(
                    0,
                    host.get("ReservedMemoryMB", 0)
                    - int(reservation["MemoryMB"]),
                )

                self.capacity.submit_transaction([
                    (
                        "update",
                        host,
                        {
                            "etag": host.metadata["etag"],
                            "match_condition": MatchConditions.IfNotModified,
                        },
                    ),
                    (
                        "delete",
                        reservation,
                        {
                            "etag": reservation.metadata["etag"],
                            "match_condition": MatchConditions.IfNotModified,
                        },
                    ),
                ])
                return True

            except ResourceModifiedError:
                if attempt == 4:
                    return False
            except ResourceNotFoundError:
                return False

        return False

