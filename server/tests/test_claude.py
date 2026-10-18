"""The Claude seam: structured JSON requests the current models accept, and how answers are read."""
import dataclasses
from types import SimpleNamespace

import anthropic
import pytest

from app import claude
from app.ingest import batch, llm
from app.shopping import service


class FakeMessages:
    def __init__(self, reply):
        self.reply = reply
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return self.reply


def message(*blocks, stop_reason="end_turn"):
    return SimpleNamespace(content=list(blocks), stop_reason=stop_reason)


def text(value):
    return SimpleNamespace(type="text", text=value)


def thinking():
    return SimpleNamespace(type="thinking", thinking="…")


@pytest.fixture
def fake_client(monkeypatch):
    def install(reply):
        messages = FakeMessages(reply)
        monkeypatch.setattr(anthropic, "Anthropic", lambda **_: SimpleNamespace(messages=messages))
        monkeypatch.setattr(claude, "config", dataclasses.replace(claude.config, anthropic_api_key="k"))
        monkeypatch.setattr(llm, "config", dataclasses.replace(llm.config, anthropic_api_key="k"))
        return messages

    return install


def test_suggest_tags_reads_the_json_answer_after_thinking(fake_client):
    fake_client(message(thinking(), text('{"tags": ["Chicken", "butter chicken", "curry"]}')))

    tags = llm.suggest_tags("Butter chicken", "dinner", "", ["chicken"], existing_tags=["curry"])

    assert tags == ["chicken", "butter-chicken", "curry"]


def test_requests_never_force_a_tool(fake_client):
    # Claude Sonnet 5.5 rejects tool_choice "tool"/"any" with a 400.
    messages = fake_client(message(text('{"tags": []}')))

    llm.suggest_tags("Toast", "snack", "", [], existing_tags=[])

    request = messages.requests[0]
    assert "tool_choice" not in request and "tools" not in request
    assert request["output_config"]["format"]["type"] == "json_schema"


@pytest.mark.parametrize("stop_reason, reply, reason", [
    ("refusal", "", "declined"),
    ("max_tokens", '{"tags": ["chi', "cut off"),
    ("end_turn", "not json", "not valid JSON"),
    ("end_turn", "[]", "not valid JSON"),
])
def test_unusable_answers_raise_a_readable_error(fake_client, stop_reason, reply, reason):
    fake_client(message(text(reply), stop_reason=stop_reason))

    with pytest.raises(claude.NoResult, match=reason):
        claude.call_json("system", [{"type": "text", "text": "x"}], llm.TAGS_SCHEMA, timeout=5)


def _schema_problems(node, path="$"):
    """Structured outputs reject open objects and type arrays with a 400."""
    problems = []
    if isinstance(node, dict):
        if node.get("type") == "object" and node.get("additionalProperties") is not False:
            problems.append(f"{path}: object without additionalProperties: false")
        if isinstance(node.get("type"), list):
            problems.append(f"{path}: type array")
        for key, value in node.items():
            problems += _schema_problems(value, f"{path}.{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            problems += _schema_problems(value, f"{path}[{i}]")
    return problems


@pytest.mark.parametrize("schema", [llm.RECIPE_SCHEMA, llm.TAGS_SCHEMA, batch.GROUPS_SCHEMA, service.LIST_SCHEMA],
                         ids=["recipe", "tags", "groups", "shopping"])
def test_schemas_are_accepted_by_structured_outputs(schema):
    assert _schema_problems(schema) == []
