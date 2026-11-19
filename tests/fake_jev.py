"""A stand-in for TypeSafe's System One API: answers by keyword and records every request body."""

from __future__ import annotations

import json
from typing import Any

import httpx


class FakeJev:
    def __init__(
        self,
        intent: str = "follow_flow",
        time: str = "last_hour",
        instance: str | None = None,
        errors_only: bool = True,
        confidence: float = 0.9,
        relevant: tuple[str, ...] = (),
        status: int = 200,
        keywords: tuple[str, ...] = (),
    ):
        self.keywords = keywords  # the words it gives when asked for some (Jev itself is never asked)
        self.intent, self.time, self.instance = intent, time, instance
        self.errors_only, self.confidence = errors_only, confidence
        self.relevant = relevant  # a candidate containing one of these words is related to the request
        self.status = status
        self.bodies: list[dict[str, Any]] = []

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/systemone", request.url
        assert request.headers["authorization"] == "Bearer fake-typesafe-key"
        body = json.loads(request.content)
        self.bodies.append(body)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "nope"})
        answers = self.answers_for(body["state"], body["questions"])
        return httpx.Response(200, json={"model": "jev-latest", "answers": answers})

    def answers_for(self, state: Any, questions: dict[str, Any]) -> dict[str, Any]:
        """What this stand-in chooses, whatever the wire it is asked over."""
        answers: dict[str, Any] = {}
        for name in questions:
            if name == "intent":
                answers[name] = self.choice(self.intent)
            elif name == "time":
                answers[name] = self.choice(self.time)
            elif name == "instance":
                answers[name] = self.choice(self.instance or next(iter(questions[name]["criteria"])))
            elif name == "errors_only":
                answers[name] = {"type": "noul", "noul": 0.95 if self.errors_only else 0.05}
            elif name == "ok":  # the key test of login --ask
                answers[name] = {"type": "noul", "noul": 0.9}
            elif name == "keywords":
                answers[name] = {"type": "words", "words": list(self.keywords)}
            else:  # c<i>: is candidates[i] related to the request
                text = state["candidates"][int(name[1:])]
                hit = any(word in text for word in self.relevant)
                answers[name] = {"type": "noul", "noul": 0.95 if hit else 0.1}
        return answers

    def choice(self, value: str) -> dict[str, Any]:
        return {"type": "choice", "choice": value, "confidence": self.confidence, "probabilities": {value: 1.0}}
