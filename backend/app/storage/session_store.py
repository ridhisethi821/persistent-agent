
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = Path(os.getenv("AGENT_DATA_DIR", str(PROJECT_ROOT / "data")))
DB_PATH = DATA_DIR / "agent.db"
WORKSPACE_DIR = DATA_DIR / "workspaces"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_connection():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_database():
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

    with get_connection() as connection:
        connection.execute("PRAGMA journal_mode = WAL")

        connection.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                status TEXT NOT NULL,
                workspace_relpath TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                claude_session_id TEXT,
                execution_provider TEXT,
                execution_resource_id TEXT,
                execution_host_id TEXT,
                execution_status TEXT NOT NULL DEFAULT 'unassigned'
            )
        """)

        connection.execute("""
            CREATE TABLE IF NOT EXISTS execution_hosts (
                host_id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                max_sessions INTEGER NOT NULL,
                max_memory_mb INTEGER NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1
            )
        """)

        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(sessions)"
            ).fetchall()
        }

        migrations = {
            "claude_session_id": (
                "ALTER TABLE sessions ADD COLUMN claude_session_id TEXT"
            ),
            "execution_provider": (
                "ALTER TABLE sessions ADD COLUMN execution_provider TEXT"
            ),
            "execution_resource_id": (
                "ALTER TABLE sessions ADD COLUMN execution_resource_id TEXT"
            ),
            "execution_host_id": (
                "ALTER TABLE sessions ADD COLUMN execution_host_id TEXT"
            ),
            "execution_status": (
                "ALTER TABLE sessions ADD COLUMN execution_status "
                "TEXT NOT NULL DEFAULT 'unassigned'"
            ),
        }

        for column, sql in migrations.items():
            if column not in columns:
                connection.execute(sql)


def create_session(title: str = "New chat", execution_provider=None) -> dict:
    session_id = str(uuid4())
    now = utc_now()
    workspace = WORKSPACE_DIR / session_id

    workspace.mkdir(parents=True, exist_ok=False)

    session = {
        "id": session_id,
        "title": title,
        "status": "active",
        "workspace_relpath": f"workspaces/{session_id}",
        "created_at": now,
        "updated_at": now,
        "claude_session_id": None,
        "execution_provider": execution_provider,
        "execution_resource_id": None,
        "execution_host_id": None,
        "execution_status": "unassigned",
    }

    try:
        with get_connection() as connection:
            connection.execute(
                """
                INSERT INTO sessions (
                    id,
                    title,
                    status,
                    workspace_relpath,
                    created_at,
                    updated_at,
                    claude_session_id,
                    execution_provider,
                    execution_resource_id,
                    execution_host_id,
                    execution_status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session["id"],
                    session["title"],
                    session["status"],
                    session["workspace_relpath"],
                    session["created_at"],
                    session["updated_at"],
                    None,
                    session["execution_provider"],
                    None,
                    None,
                    "unassigned",
                ),
            )
    except Exception:
        workspace.rmdir()
        raise

    return session


def get_session(session_id: str) -> dict | None:
    with get_connection() as connection:
        row = connection.execute(
            "SELECT * FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()

    return dict(row) if row else None


def list_sessions() -> list[dict]:
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT * FROM sessions ORDER BY updated_at DESC"
        ).fetchall()

    return [dict(row) for row in rows]


def update_claude_session_id(
    session_id: str,
    claude_session_id: str,
) -> bool:
    now = utc_now()

    with get_connection() as connection:
        cursor = connection.execute(
            """
            UPDATE sessions
            SET claude_session_id = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (claude_session_id, now, session_id),
        )

    return cursor.rowcount > 0


def update_execution_assignment(
    session_id: str,
    provider: str,
    resource_id: str,
    status: str = "ready",
    host_id: str | None = None,
) -> bool:
    now = utc_now()

    with get_connection() as connection:
        cursor = connection.execute(
            """
            UPDATE sessions
            SET execution_provider = ?,
                execution_resource_id = ?,
                execution_host_id = ?,
                execution_status = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                provider,
                resource_id,
                host_id,
                status,
                now,
                session_id,
            ),
        )

    return cursor.rowcount > 0


def update_execution_status(
    session_id: str,
    status: str,
) -> bool:
    now = utc_now()

    with get_connection() as connection:
        cursor = connection.execute(
            """
            UPDATE sessions
            SET execution_status = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (status, now, session_id),
        )

    return cursor.rowcount > 0


def clear_execution_assignment(session_id: str) -> bool:
    now = utc_now()

    with get_connection() as connection:
        cursor = connection.execute(
            """
            UPDATE sessions
            SET execution_provider = NULL,
                execution_resource_id = NULL,
                execution_host_id = NULL,
                execution_status = 'unassigned',
                updated_at = ?
            WHERE id = ?
            """,
            (now, session_id),
        )

    return cursor.rowcount > 0

def register_execution_host(
    host_id: str,
    provider: str,
    max_sessions: int,
    max_memory_mb: int,
    enabled: bool = True,
) -> None:
    if max_sessions < 1 or max_memory_mb < 1:
        raise ValueError("Host capacity values must be positive")

    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO execution_hosts (
                host_id, provider, max_sessions,
                max_memory_mb, enabled
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(host_id) DO UPDATE SET
                provider = excluded.provider,
                max_sessions = excluded.max_sessions,
                max_memory_mb = excluded.max_memory_mb,
                enabled = excluded.enabled
            """,
            (
                host_id,
                provider,
                max_sessions,
                max_memory_mb,
                int(enabled),
            ),
        )


def list_execution_hosts() -> list[dict]:
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT * FROM execution_hosts ORDER BY host_id"
        ).fetchall()

        hosts = []
        for row in rows:
            host = dict(row)

            count = connection.execute(
                """
                SELECT COUNT(*) AS active_sessions
                FROM sessions
                WHERE execution_host_id = ?
                """,
                (host["host_id"],),
            ).fetchone()

            host["active_sessions"] = count["active_sessions"]
            host["enabled"] = bool(host["enabled"])
            hosts.append(host)

        return hosts


def reserve_execution_host(
    session_id: str,
    provider: str,
    required_memory_mb: int,
) -> dict:
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")

        session = connection.execute(
            """
            SELECT execution_host_id
            FROM sessions
            WHERE id = ?
            """,
            (session_id,),
        ).fetchone()

        if not session:
            raise ValueError(f"Session not found: {session_id}")

        # Reuse an existing assignment rather than reserving twice.
        if session["execution_host_id"]:
            existing = connection.execute(
                """
                SELECT *
                FROM execution_hosts
                WHERE host_id = ?
                """,
                (session["execution_host_id"],),
            ).fetchone()

            if not existing:
                raise RuntimeError("Previously assigned host no longer exists")

            return dict(existing)

        hosts = connection.execute(
            """
            SELECT
                h.*,
                (
                    SELECT COUNT(*)
                    FROM sessions s
                    WHERE s.execution_host_id = h.host_id
                ) AS active_sessions
            FROM execution_hosts h
            WHERE h.provider = ?
              AND h.enabled = 1
              AND h.max_memory_mb >= ?
            ORDER BY active_sessions ASC, h.host_id ASC
            """,
            (provider, required_memory_mb),
        ).fetchall()

        for row in hosts:
            host = dict(row)

            if host["active_sessions"] >= host["max_sessions"]:
                continue

            connection.execute(
                """
                UPDATE sessions
                SET execution_provider = ?,
                    execution_host_id = ?,
                    execution_status = 'starting',
                    updated_at = ?
                WHERE id = ?
                """,
                (provider, host["host_id"], utc_now(), session_id),
            )

            return host

        raise RuntimeError(
            f"No available execution host for provider '{provider}'"
        )