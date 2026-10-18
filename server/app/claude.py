"""One structured-output Claude call shared by import, tagging, batch grouping and shopping lists.

Uses JSON outputs (output_config.format) rather than a forced tool: Claude Sonnet 5.5 rejects
tool_choice "tool"/"any", and JSON outputs guarantee the schema on every model that has them.
"""
import json

from app.config import config


class NoResult(Exception):
    """Claude answered, but not with a usable JSON object."""


def call_json(system: str, content: list[dict], schema: dict, *, timeout: float, max_tokens: int = 8192) -> dict:
    """Return Claude's answer as a dict matching schema. Raises anthropic.APIError or NoResult.

    max_tokens leaves room for adaptive thinking, which counts towards it.
    """
    import anthropic

    client = anthropic.Anthropic(api_key=config.anthropic_api_key, timeout=timeout)
    msg = client.messages.create(
        model=config.llm_model,
        max_tokens=max_tokens,
        system=system,
        output_config={"format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": content}],
    )
    if msg.stop_reason == "refusal":
        raise NoResult("Claude declined to answer")
    if msg.stop_reason == "max_tokens":
        raise NoResult("the answer was cut off")
    answer = "".join(block.text for block in msg.content if block.type == "text")
    try:
        out = json.loads(answer)
    except ValueError:
        out = None
    if not isinstance(out, dict):
        raise NoResult("the answer was not valid JSON")
    return out
