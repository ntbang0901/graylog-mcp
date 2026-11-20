"""A stand-in for an OpenAI-compatible Chat Completions endpoint: replies as told and records every request."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx

from tests.fake_jev import FakeJev


class FakeOpenAI:
    """``replies``: for each request in turn, an HTTP status or the text of the model's message. When they run
    out (or none are given) ``chooser`` answers every question, wrapped the way a chatty model would."""

    def __init__(
        self,
        replies: list[int | str] | None = None,
        chooser: FakeJev | None = None,
        key: str | None = "fake-gateway-key",
        wrap: Callable[[str], str] = "Sure, here it is:\n```json\n{}\n```".format,
    ):
        self.replies = list(replies or [])
        self.chooser = chooser or FakeJev()
        self.key, self.wrap = key, wrap
        self.bodies: list[dict[str, Any]] = []
        self.sent: list[bytes] = []  # every request body as it went over the wire

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions"), request.url
        assert request.headers.get("authorization") == (f"Bearer {self.key}" if self.key else None)
        body = json.loads(request.content)
        self.bodies.append(body)
        self.sent.append(request.content)
        reply = self.replies.pop(0) if self.replies else None
        if isinstance(reply, int):
            return httpx.Response(reply, json={"error": {"message": "nope"}})
        if reply is None:
            asked = json.loads(body["messages"][-1]["content"])
            answers = self.chooser.answers_for(asked["state"], asked["questions"])
            reply = self.wrap(json.dumps({"answers": answers}))
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": reply}}]})
