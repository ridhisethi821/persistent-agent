
import asyncio
import json
from threading import Lock
from typing import Any

from claude_agent_sdk import tool, create_sdk_mcp_server

from app.execution.allocator import SessionExecutionAllocator


_allocator = None
_allocator_lock = Lock()


def get_allocator() -> SessionExecutionAllocator:
    """Return the shared execution allocator."""

    global _allocator

    if _allocator is None:
        with _allocator_lock:
            if _allocator is None:
                _allocator = SessionExecutionAllocator()

    return _allocator


def create_execution_server(session: dict[str, Any]):
    """Create an MCP execution tool bound to one application session."""

    allocator = get_allocator()

    @tool(
        name="run_command",
        description=(
            "Execute a Bash or Python command inside this session's "
            "isolated execution workspace. The working directory is "
            "/workspace. Use relative paths to read and write workspace "
            "files. The workspace persists between commands and "
            "container restarts. Network access is disabled. "
            "Return useful command output."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Bash or Python command to execute.",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 120,
                    "description": "Maximum execution time in seconds.",
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    )
    async def run_command(args: dict[str, Any]) -> dict[str, Any]:
        command = args.get("command")

        if not isinstance(command, str) or not command.strip():
            return {
                "content": [
                    {
                        "type": "text",
                        "text": "Error: command cannot be empty.",
                    }
                ],
                "isError": True,
            }

        if len(command) > 12000:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": "Error: command is too long.",
                    }
                ],
                "isError": True,
            }

        timeout = args.get("timeout_seconds", 30)

        if isinstance(timeout, bool) or not isinstance(timeout, int):
            timeout = 30

        timeout = max(1, min(timeout, 120))

        try:
            result = await asyncio.to_thread(
                allocator.run_command,
                session,
                command,
                timeout,
            )

            output = {
                "exit_code": result["exit_code"],
                "stdout": result["stdout"],
                "stderr": result["stderr"],
                "truncated": result["truncated"],
            }

            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(output, ensure_ascii=False),
                    }
                ]
            }

        except Exception as exc:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Execution failed: "
                            f"{type(exc).__name__}. "
                            "Check the execution provider and session workspace."
                        ),
                    }
                ],
                "isError": True,
            }

    return create_sdk_mcp_server(
        name="workspace",
        tools=[run_command],
    )



def close_allocator() -> None:
    """Close the shared execution allocator during application shutdown."""
    global _allocator
    with _allocator_lock:
        allocator = _allocator
        _allocator = None
    if allocator is not None:
        allocator.close()
