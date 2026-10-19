"""The Claude seam: structured JSON requests the current models accept, and how answers are read."""
import dataclasses
from types import SimpleNamespace

import anthropic
import pytest
from PIL import Image

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


RECIPE_REPLY = ('{"title": "Butter chicken", "type": "dinner", "tags": [], "description": "", '
                '"ingredients": ["3 chicken breasts"], "steps": ["Cube the chicken."], "is_recipe": true, '
                '"photo_slide": %s}')


def _slides(tmp_path, n):
    paths = []
    for i in range(n):
        paths.append(tmp_path / f"slide-{i}.jpg")
        Image.new("RGB", (9, 16), "white").save(paths[-1], "JPEG")
    return paths


def test_slides_are_numbered_for_claude_and_its_dish_slide_comes_back_zero_based(fake_client, tmp_path):
    messages = fake_client(message(text(RECIPE_REPLY % 3)))

    result = llm.extract({"caption": "so good #fyp"}, "", [], "tiktok", [], slides=_slides(tmp_path, 3))

    request = messages.requests[0]
    content = request["messages"][0]["content"]
    assert [b["text"] for b in content[1:] if b["type"] == "text"] == ["Slide 1", "Slide 2", "Slide 3"]
    assert [b["type"] for b in content[1:]] == ["text", "image"] * 3
    assert request["output_config"]["format"]["schema"] == llm.SLIDES_SCHEMA
    assert "slides" in request["system"]
    assert (result["ingredients"], result["photo_slide"]) == (["3 chicken breasts"], 2)


@pytest.mark.parametrize("answer", ["null", "0", "4"])
def test_no_dish_slide_when_claude_names_none_or_one_that_doesnt_exist(fake_client, tmp_path, answer):
    fake_client(message(text(RECIPE_REPLY % answer)))

    result = llm.extract({}, "", [], "tiktok", [], slides=_slides(tmp_path, 3))

    assert result["photo_slide"] is None


def test_posts_with_more_slides_than_tiktok_allows_are_cut_with_a_note(fake_client, tmp_path):
    messages = fake_client(message(text(RECIPE_REPLY % "null")))

    result = llm.extract({}, "", [], "tiktok", [], slides=_slides(tmp_path, llm.MAX_SLIDES + 1))

    images = [b for b in messages.requests[0]["messages"][0]["content"] if b["type"] == "image"]
    assert len(images) == llm.MAX_SLIDES
    assert result["note"] == f"Only the first {llm.MAX_SLIDES} slides were read."


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


@pytest.mark.parametrize("schema", [llm.RECIPE_SCHEMA, llm.SLIDES_SCHEMA, llm.TAGS_SCHEMA, batch.GROUPS_SCHEMA,
                                    service.LIST_SCHEMA],
                         ids=["recipe", "slides", "tags", "groups", "shopping"])
def test_schemas_are_accepted_by_structured_outputs(schema):
    assert _schema_problems(schema) == []
