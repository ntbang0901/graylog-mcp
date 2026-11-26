"""What a model is given: the instructions and every tool declaration, compared with a reviewed snapshot."""

from __future__ import annotations

import json
import os
from pathlib import Path

from graylog_mcp.server import INSTRUCTIONS, build_server

SNAPSHOT = Path(__file__).parent / "snapshots" / "model_surface.json"
SNAPSHOT_ON = Path(__file__).parent / "snapshots" / "model_surface_investigations.json"


async def _surface(make_app, monkeypatch, investigations: str) -> tuple[str, int]:
    monkeypatch.setenv("GRAYLOG_MCP_INVESTIGATIONS", investigations)
    app, _ = make_app()
    server = build_server(app)
    declared = [t.model_dump(mode="json", exclude_none=True) for t in await server.list_tools()]
    offered = [p.model_dump(mode="json", exclude_none=True) for p in await server.list_prompts()]
    surface = {"instructions": server.instructions, "tools": declared, "prompts": offered}
    text = json.dumps(surface, indent=2, ensure_ascii=False) + "\n"
    return text, len(declared)


def _check(text: str, path: Path) -> None:
    if os.environ.get("GRAYLOG_MCP_UPDATE_SNAPSHOT"):
        path.parent.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8")
    assert text == path.read_text(encoding="utf-8"), (
        "the instructions or a tool declaration changed: review the diff, then rerun with "
        "GRAYLOG_MCP_UPDATE_SNAPSHOT=1 if the change is intended"
    )


async def test_with_investigations_off_what_the_model_receives_is_what_it_always_was(make_app, monkeypatch):
    """The switch removes the two tools, the three arguments and the instructions paragraph: nothing else changed."""
    text, count = await _surface(make_app, monkeypatch, "off")
    assert count == 11 and json.loads(text)["instructions"] == INSTRUCTIONS
    _check(text, SNAPSHOT)


async def test_what_the_model_receives_with_investigations(make_app, monkeypatch):
    text, count = await _surface(make_app, monkeypatch, "on")
    assert count == 13
    _check(text, SNAPSHOT_ON)
