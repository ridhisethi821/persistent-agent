from pathlib import Path
import os

from claude_agent_sdk import (
    ClaudeAgentOptions,
    AssistantMessage,
    ResultMessage,
    TextBlock,
    query,
)

from app.agent.execution_tools import create_execution_server
from app.agent.document_input import build_agent_prompt
from app.storage.claude_transcript_store import get_claude_transcript_store


MODEL_NAME = "claude-haiku-5-5"
PROJECT_ROOT = Path(__file__).resolve().parents[3]


async def send_message(
    prompt: str,
    session: dict,
    previous_session_id: str | None = None,
    attachment_filename: str | None = None,
) -> dict:

    if not os.getenv("ANTHROPIC_API_KEY"):
        raise RuntimeError("Anthropic API key is not configured.")

    execution_server = create_execution_server(session)

    options = ClaudeAgentOptions(
        model=MODEL_NAME,
        cwd=str(PROJECT_ROOT),
        mcp_servers={
            "workspace": execution_server,
        },
        allowed_tools=[
            "mcp__workspace__run_command",
        ],
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
        resume=previous_session_id,
        session_store=get_claude_transcript_store(),
        session_store_flush="eager",
    )

    agent_prompt = await build_agent_prompt(
        prompt=prompt,
        session=session,
        attachment_filename=attachment_filename,
    )

    response_text = []
    claude_session_id = previous_session_id

    async for message in query(
        prompt=agent_prompt,
        options=options,
    ):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    response_text.append(block.text)

        elif isinstance(message, ResultMessage):
            if message.is_error:
                raise RuntimeError("Claude agent returned an error.")

            claude_session_id = message.session_id

    return {
        "response": "\n".join(response_text),
        "claude_session_id": claude_session_id,
    }