"""What a model is given: the instructions and every tool declaration, compared with a reviewed snapshot."""

from __future__ import annotations

import json
import os
from pathlib import Path

from graylog_mcp.server import INSTRUCTIONS, build_server

SNAPSHOT = Path(__file__).parent / "snapshots" / "model_surface.json"


async def test_what_the_model_receives_is_unchanged(make_app, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "fake-typesafe-key")  # ask is declared only with a key
    app, _ = make_app()
    declared = [t.model_dump(mode="json", exclude_none=True) for t in await build_server(app).list_tools()]
    assert len(declared) == 10
    text = json.dumps({"instructions": INSTRUCTIONS, "tools": declared}, indent=2, ensure_ascii=False) + "\n"
    if os.environ.get("GRAYLOG_MCP_UPDATE_SNAPSHOT"):
        SNAPSHOT.parent.mkdir(exist_ok=True)
        SNAPSHOT.write_text(text, encoding="utf-8")
    assert text == SNAPSHOT.read_text(encoding="utf-8"), (
        "the instructions or a tool declaration changed: review the diff, then rerun with "
        "GRAYLOG_MCP_UPDATE_SNAPSHOT=1 if the change is intended"
    )
