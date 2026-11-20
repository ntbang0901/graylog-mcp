"""What a model is given: the instructions and every tool declaration, compared with a reviewed snapshot."""

from __future__ import annotations

import json
import os
from pathlib import Path

from graylog_mcp.server import INSTRUCTIONS, build_server

SNAPSHOT = Path(__file__).parent / "snapshots" / "model_surface.json"


async def test_what_the_model_receives_is_unchanged(make_app):
    app, _ = make_app()
    server = build_server(app)
    declared = [t.model_dump(mode="json", exclude_none=True) for t in await server.list_tools()]
    assert len(declared) == 10
    offered = [p.model_dump(mode="json", exclude_none=True) for p in await server.list_prompts()]
    surface = {"instructions": INSTRUCTIONS, "tools": declared, "prompts": offered}
    text = json.dumps(surface, indent=2, ensure_ascii=False) + "\n"
    if os.environ.get("GRAYLOG_MCP_UPDATE_SNAPSHOT"):
        SNAPSHOT.parent.mkdir(exist_ok=True)
        SNAPSHOT.write_text(text, encoding="utf-8")
    assert text == SNAPSHOT.read_text(encoding="utf-8"), (
        "the instructions or a tool declaration changed: review the diff, then rerun with "
        "GRAYLOG_MCP_UPDATE_SNAPSHOT=1 if the change is intended"
    )
