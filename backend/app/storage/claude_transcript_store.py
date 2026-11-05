import os

_store = None


def initialize_claude_transcript_store():
    """Initialize the configured Claude transcript storage backend."""
    global _store

    backend = os.getenv(
        "CLAUDE_TRANSCRIPT_STORE_BACKEND", "local"
    ).lower()

    if backend == "local":
        _store = None
        return

    if backend == "azure_blob":
        from app.storage.azure_claude_session_store import (
            AzureClaudeSessionStore,
        )
        _store = AzureClaudeSessionStore()
        return

    raise ValueError(
        f"Unsupported Claude transcript store backend: {backend}"
    )


def get_claude_transcript_store():
    """Return the shared transcript store, or None in local mode."""
    global _store

    backend = os.getenv(
        "CLAUDE_TRANSCRIPT_STORE_BACKEND", "local"
    ).lower()

    if backend == "local":
        return None

    if backend == "azure_blob":
        if _store is None:
            initialize_claude_transcript_store()
        return _store

    raise ValueError(
        f"Unsupported Claude transcript store backend: {backend}"
    )


async def close_claude_transcript_store():
    """Close shared Azure clients during application shutdown."""
    global _store

    if _store is not None:
        await _store.close()
        _store = None
