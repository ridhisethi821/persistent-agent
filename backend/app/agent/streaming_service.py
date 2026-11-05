
import json
from pathlib import Path
from typing import Any, AsyncIterator

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ResultMessage,
    UserMessage,
    AssistantMessage,
    ToolResultBlock,
    query,
)

from app.agent.document_input import build_agent_prompt
from app.agent.execution_tools import create_execution_server
from app.storage.claude_transcript_store import get_claude_transcript_store


MODEL_NAME = "claude-haiku-5-5"
PROJECT_ROOT = Path(__file__).resolve().parents[3]


async def stream_claude_message(
    prompt: str,
    session: dict[str, Any],
    previous_session_id: str | None = None,
    attachment_filename: str | None = None,
) -> AsyncIterator[dict[str, Any]]:

    agent_prompt = await build_agent_prompt(
        prompt,
        session,
        attachment_filename,
    )

    execution_server = create_execution_server(session)

    options = ClaudeAgentOptions(
        model=MODEL_NAME,
        cwd=str(PROJECT_ROOT),
        resume=previous_session_id,
        include_partial_messages=True,
        mcp_servers={"workspace": execution_server},
        allowed_tools=["mcp__workspace__run_command"],
        tools=[],
        disallowed_tools=[
            "Bash",
            "Read",
            "Write",
            "Edit",
            "Glob",
            "Grep",
            "WebSearch",
            "WebFetch",
        ],
        max_turns=8,
        session_store=get_claude_transcript_store(),
        session_store_flush="eager",
    )

    current_tool_name = None
    current_tool_id = None
    accumulated_text = ""

    async for message in query(
        prompt=agent_prompt,
        options=options,
    ):
        if message.__class__.__name__ == "StreamEvent":
            event_data = getattr(message, "event", {}) or {}
            event_type = event_data.get("type")

            if event_type == "content_block_start":
                content_block = event_data.get("content_block", {})

                if content_block.get("type") == "tool_use":
                    current_tool_name = content_block.get("name")
                    current_tool_id = content_block.get("id")

                    yield {
                        "type": "tool_start",
                        "tool_name": current_tool_name,
                        "tool_id": current_tool_id,
                    }

            elif event_type == "content_block_delta":
                delta = event_data.get("delta", {})
                delta_type = delta.get("type")

                if delta_type == "text_delta":
                    text_delta = delta.get("text", "")

                    if text_delta:
                        accumulated_text += text_delta

                        yield {
                            "type": "text",
                            "text": text_delta,
                        }

                elif delta_type == "input_json_delta":
                    partial_json = delta.get("partial_json", "")

                    if partial_json:
                        yield {
                            "type": "tool_input",
                            "tool_name": current_tool_name,
                            "tool_id": current_tool_id,
                            "partial_json": partial_json,
                        }

            elif event_type == "content_block_stop":
                current_tool_name = None
                current_tool_id = None

        elif isinstance(message, AssistantMessage):
            # Tool results are emitted from UserMessage.tool_use_result below.
            pass
        elif isinstance(message, UserMessage):
            # MCP tool results are represented as ToolResultBlock content.
            # Use the block's own ID; parent_tool_use_id is often None
            # for top-level MCP tool results.
            if isinstance(message.content, list):
                for block in message.content:
                    if isinstance(block, ToolResultBlock):
                        yield {
                            "type": "tool_result",
                            "tool_id": block.tool_use_id,
                            "content": block.content,
                            "is_error": block.is_error,
                        }

            # Retain the SDK's auxiliary result metadata as a fallback
            # for messages that do not contain a ToolResultBlock.
            elif message.tool_use_result is not None:
                yield {
                    "type": "tool_result",
                    "tool_id": message.parent_tool_use_id,
                    "content": message.tool_use_result,
                    "is_error": False,
                }
        elif isinstance(message, ResultMessage):
            if message.is_error:
                yield {
                    "type": "error",
                    "message": "Claude agent run failed.",
                    "errors": message.errors or [],
                    "claude_session_id": message.session_id,
                }
                continue
            yield {
                "type": "done",
                "response": accumulated_text,
                "claude_session_id": message.session_id,
            }
