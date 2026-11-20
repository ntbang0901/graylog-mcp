"""The prompts a user picks in their chat client: built-in ones for a task, and a team's own."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from graylog_mcp import bundle
from graylog_mcp.config import ConfigError
from graylog_mcp.server import build_server
from graylog_mcp.tools import App
from tests.conftest import make_config
from tests.fake_graylog import FakeGraylog

RELEASE = {
    "title": "Kiểm tra sau release",
    "description": "Errors of one service since its release",
    "text": "Check {service} since {since}: count errors with aggregate, then scan. Use {{braces}} as written.",
    "arguments": {"service": "the service released", "since": {"description": "when", "default": "30m"}},
}


def server_with(**overrides):
    app = App.create(make_config(**overrides), transport=FakeGraylog().transport)
    return build_server(app)


async def text_of(server, name: str, args: dict) -> str:
    result = await server.get_prompt(name, args)
    return "\n".join(m.content.text for m in result.messages)


async def test_the_built_in_prompts_start_from_words_and_codes():
    server = server_with()
    names = [p.name for p in await server.list_prompts()]
    assert names == ["scan", "find_error", "trace_code", "check_after_change", "ticket_evidence"]
    found = await text_of(server, "find_error", {"description": "thanh toán bị lỗi sáng nay", "environment": "prod"})
    assert '"thanh toán bị lỗi sáng nay"' in found and "Call ask" in found and "instance" in found
    assert "language the user wrote in" in found
    traced = await text_of(server, "trace_code", {"code": "ORD-1001"})
    assert "trace_request with trace_id='ORD-1001' and range='24h'" in traced
    ticket = await text_of(server, "ticket_evidence", {"problem": "login fails"})
    assert "Markdown" in ticket and "the query that ran" in ticket
    with pytest.raises(ValueError, match="description"):
        await server.get_prompt("find_error", {})


async def test_a_team_prompt_fills_its_arguments_and_their_defaults():
    server = server_with(prompts={"release_check": RELEASE})
    listed = {p.name: p for p in await server.list_prompts()}["release_check"]
    assert listed.title == "Kiểm tra sau release"
    assert [(a.name, a.required) for a in listed.arguments] == [("service", True), ("since", False)]
    text = await text_of(server, "release_check", {"service": "checkout"})
    assert text == "Check checkout since 30m: count errors with aggregate, then scan. Use {braces} as written."
    with pytest.raises(ValueError, match="service"):
        await server.get_prompt("release_check", {"since": "1h"})


@pytest.mark.parametrize(
    ("prompt", "error"),
    [
        ({"text": "Check {service}"}, r"\{service\} is not one of its arguments \(none\)"),
        ({"text": "Check {service.name}", "arguments": {"service": "x"}}, "not one of its arguments"),
        ({"text": "Check {", "arguments": {}}, "literal brace"),
        ({"arguments": {}}, "text: expected"),
        ({"text": "x", "arguments": {"bad name": "x"}}, "names may only"),
        ({"text": "x", "tool": "search_logs"}, "unknown key"),
    ],
)
def test_a_team_prompt_that_cannot_render_is_refused(prompt, error):
    with pytest.raises(ConfigError, match=error):
        make_config(prompts={"mine": prompt})


def test_a_team_prompt_may_not_take_a_built_in_name():
    with pytest.raises(ConfigError, match="built-in"):
        make_config(prompts={"find_error": RELEASE})


async def test_the_example_config_holds_a_team_prompt_that_renders():
    example = Path(__file__).resolve().parents[1] / "examples" / "config.toml"
    data = tomllib.loads(example.read_text(encoding="utf-8"))
    text = await text_of(server_with(prompts=data["prompts"]), "release_check", {"service": "checkout"})
    assert text.startswith("Check checkout on prod since 30m:")


def test_a_team_bundle_carries_the_team_prompts():
    text, dropped = bundle.export_bundle({"prompts": {"release_check": RELEASE}}, "acme")
    assert "release_check" in text and dropped == []
