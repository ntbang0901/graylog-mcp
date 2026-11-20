"""The models ``ask`` may ask for what its rules could not read, behind one contract.

A provider answers questions about a state: a choice from a list the server gives, a probability, or plain
words. Whatever kind it is, the same checks decide what of its answer is believed, so nothing a model writes
reaches a query or a tool argument except as one of the choices it was offered. Who is asked follows the
configured order; the next one only when the one before failed, so the text of one question reaches one place.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from collections.abc import Iterator, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Protocol

from graylog_mcp.client import GraylogError, spans_recorded
from graylog_mcp.config import ProviderConfig

UNDERSTAND, SCORE = "understand", "score"
# what the instance's provider_data must be at least for a task: scoring hands over text of the logs
_NEEDS = {UNDERSTAND: ("question_only", "question_and_logs"), SCORE: ("question_and_logs",)}
_WORD = re.compile(r"[a-z0-9][a-z0-9._-]{1,39}")
_THOUGHTS = re.compile(r"<think>.*?</think>", re.DOTALL)


# Requests and bytes sent to each provider for the question being answered: provider -> [requests, bytes]. Set
# by ask for one question, like client.requests_sent, so two questions at once each count their own.
handed_over: ContextVar[dict[str, list[int]] | None] = ContextVar("handed_over", default=None)
# What was handed to each provider asked, when someone shows it (the admin page's "what was sent"): provider,
# task, the state and the questions, as redacted as the provider got them.
sent_payloads: ContextVar[list[dict[str, Any]] | None] = ContextVar("sent_payloads", default=None)


class ProviderError(GraylogError):
    """A provider could not be asked or did not answer. Its message never repeats what was sent or returned."""


@contextlib.contextmanager
def recorded(provider: str, method: str, path: str) -> Iterator[dict[str, Any]]:
    """The span of one request to a provider in the call's trace; the caller fills in status and bytes_sent."""
    spans, began = spans_recorded.get(), time.monotonic()
    span: dict[str, Any] = {"service": provider, "method": method, "path": path, "wait_ms": 0}
    try:
        yield span
    except asyncio.CancelledError:
        span["status"] = "cancelled"
        raise
    finally:
        if (tally := handed_over.get()) is not None:
            used = tally.setdefault(provider, [0, 0])
            used[0] += int(span.get("attempts", 1))
            used[1] += int(span.get("bytes_sent", 0))
        if spans is not None:
            span.update(start_ms=spans.ms_since_start(began), ms=int((time.monotonic() - began) * 1000))
            spans.add(span)


class Provider(Protocol):
    requests: int
    bytes_sent: int
    words: bool  # answers questions of type "words"

    async def answers(self, state: Any, questions: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """An answer for each question name; raises ``ProviderError`` when there is none to read."""
        ...

    async def aclose(self) -> None: ...


@dataclass(frozen=True)
class Answered:
    provider: str
    answers: dict[str, dict[str, Any]]  # checked: only the questions whose answer could be believed


class Providers:
    def __init__(self, order: Sequence[tuple[ProviderConfig, Provider]]):
        self.order = list(order)

    @classmethod
    def create(cls, configs: Sequence[ProviderConfig], transport: Any = None) -> Providers:
        return cls([(cfg, _client(cfg, transport)) for cfg in configs])

    def _able(self, task: str, policy: str, words: bool) -> list[tuple[ProviderConfig, Provider]]:
        if policy not in _NEEDS[task]:
            return []
        return [(cfg, client) for cfg, client in self.order if task in cfg.can and (client.words or not words)]

    def may_ask(self, task: str, policy: str, words: bool = False) -> bool:
        """Whether a provider would be asked: one is configured for ``task``, and the instance's ``policy`` lets
        it have what the task hands over. ``words``: the question is of type "words"."""
        return bool(self._able(task, policy, words))

    async def ask(
        self, task: str, policy: str, state: Any, questions: dict[str, dict[str, Any]], failed: list[dict[str, str]]
    ) -> Answered | None:
        """The checked answers of the first provider that gives any; None when nobody may be asked or all failed.
        Each failure is added to ``failed`` as provider, task and reason; a provider already in it is not asked
        again, so one that is down costs a question its wait once."""
        words = any(q.get("type") == "words" for q in questions.values())
        for cfg, client in self._able(task, policy, words):
            if any(failure["provider"] == cfg.name for failure in failed):
                continue
            entry = {"provider": cfg.name, "task": task, "state": state, "questions": questions}
            if (shown := sent_payloads.get()) is not None:
                shown.append(entry)
            requests_before = client.requests
            try:
                believed = _checked(questions, await client.answers(state, questions))
                if not believed:
                    raise ProviderError("none of its answers was one of the choices it was given")
            except ProviderError as exc:
                failed.append({"provider": cfg.name, "task": task, "reason": str(exc)})
                if shown is not None and client.requests == requests_before:
                    shown.remove(entry)  # refused before sending (no key): nothing left the machine
                continue
            except Exception as exc:
                # Whatever a reply is built to break (nesting too deep to parse, a number too large to be one),
                # it breaks this provider only. The exception's own text may quote the reply: its type is said.
                reason = f"its reply could not be read ({type(exc).__name__})"
                failed.append({"provider": cfg.name, "task": task, "reason": reason})
                continue
            return Answered(cfg.name, believed)
        return None

    def usage(self) -> dict[str, tuple[int, int]]:
        """Requests and bytes sent so far, by provider."""
        return {cfg.name: (client.requests, client.bytes_sent) for cfg, client in self.order}

    async def aclose(self) -> None:
        for _, client in self.order:
            await client.aclose()


def _client(cfg: ProviderConfig, transport: Any) -> Provider:
    from graylog_mcp.ask.command import CommandClient  # they import ProviderError from here
    from graylog_mcp.ask.jev import JevClient
    from graylog_mcp.ask.openai import OpenAIClient

    if cfg.kind == "command":
        return CommandClient(cfg)
    return OpenAIClient(cfg, transport) if cfg.kind == "openai" else JevClient(cfg, transport)


def _checked(questions: dict[str, dict[str, Any]], answers: Any) -> dict[str, dict[str, Any]]:
    """What of ``answers`` may be believed: per question, an answer of its type within what was offered."""
    believed: dict[str, dict[str, Any]] = {}
    if not isinstance(answers, dict):
        return believed
    for name, question in questions.items():
        answer = answers.get(name)
        if not isinstance(answer, dict):
            continue
        kind = question.get("type")
        if kind == "choice":
            choice = answer.get("choice")
            if isinstance(choice, str) and choice in question["criteria"]:
                believed[name] = {"choice": choice, "confidence": _unit(answer.get("confidence")) or 0.0}
        elif kind == "noul":
            if (p := _unit(answer.get("noul"), clamp=False)) is not None:
                believed[name] = {"noul": p}
        elif kind == "words" and isinstance(answer.get("words"), list):
            words = [w.lower() for w in answer["words"] if isinstance(w, str) and _WORD.fullmatch(w.lower())]
            if words := list(dict.fromkeys(words))[: int(question.get("max", 8))]:
                believed[name] = {"words": words}
    return believed


def _unit(value: Any, clamp: bool = True) -> float | None:
    """A number from 0 to 1. A confidence beyond that is brought back in; a probability beyond it is no answer."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if clamp:
        return min(1.0, max(0.0, float(value)))
    return float(value) if 0 <= value <= 1 else None


UNSTATED_CONFIDENCE = 0.7  # of a choice a model gave without saying how sure it is


def read_reply(text: str) -> dict[str, Any]:
    """The answers in the text a model or a command wrote: under "answers", or the object itself. Raises
    ``ValueError`` when the text holds no JSON object."""
    found = json_object(text)
    answers = found["answers"] if isinstance(found.get("answers"), dict) else found
    for answer in answers.values():
        if isinstance(answer, dict) and "choice" in answer:
            answer.setdefault("confidence", UNSTATED_CONFIDENCE)
    return dict(answers)


def json_object(text: str) -> Any:
    """The first JSON object in a model's reply, whatever surrounds it: prose, a code fence, its thinking.
    Raises ``ValueError`` when there is none."""
    text = _THOUGHTS.sub("", text)
    if (open_thought := text.find("<think>")) >= 0:  # never closed: nothing after it is an answer
        text = text[:open_thought]
    decoder = json.JSONDecoder()
    at = text.find("{")
    while at >= 0:
        try:
            found, _ = decoder.raw_decode(text, at)
        except ValueError:
            at = text.find("{", at + 1)
            continue
        if isinstance(found, dict):
            return found
        at = text.find("{", at + 1)
    raise ValueError("no JSON object in the reply")
