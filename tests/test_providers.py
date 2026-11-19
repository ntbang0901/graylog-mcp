"""Who ``ask`` may ask for what it could not read itself: configuration, order, policy and what is believed."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from graylog_mcp.ask.command import CommandClient
from graylog_mcp.ask.openai import OpenAIClient
from graylog_mcp.ask.providers import SCORE, UNDERSTAND, ProviderError, Providers, json_object
from graylog_mcp.config import ConfigError, ProviderConfig, parse_config
from tests.conftest import make_config
from tests.fake_openai import FakeOpenAI

MAIN = {"instances": {"main": {"url": "https://graylog.test", "token_env": "TEST_GRAYLOG_TOKEN"}}}
GATEWAY = {"kind": "openai", "base_url": "http://127.0.0.1:4000/v1", "model": "acme-default"}


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "fake-typesafe-key")


@pytest.fixture(autouse=True)
def no_saved_choice(monkeypatch):
    """A developer's own key, model or endpoint must not decide these tests."""
    for name in ("TYPESAFE_API_KEY", "GRAYLOG_MCP_ASK_MODEL", "GRAYLOG_MCP_ASK_BASE_URL"):
        monkeypatch.delenv(name, raising=False)


# ------------------------------------------------------------------ configuration


def test_without_a_key_and_without_providers_there_is_no_provider():
    assert make_config().ask.providers == ()


def test_a_saved_typesafe_key_alone_is_the_jev_provider_for_both_tasks(key):
    (jev,) = make_config(ask={"model": "jev-small"}).ask.providers
    assert (jev.name, jev.kind, jev.can) == ("jev", "jev", ("understand", "score"))
    assert (jev.model, jev.base_url, jev.api_key) == ("jev-small", "https://api.typesafe.ai", "fake-typesafe-key")
    assert "fake-typesafe-key" not in repr(jev)


def test_an_empty_order_turns_every_provider_off_even_with_a_key(key):
    assert make_config(ask={"provider_order": []}).ask.providers == ()
    assert make_config(ask={"provider_order": [], "providers": {"gw": GATEWAY}}).ask.providers == ()


def test_the_order_decides_who_is_asked_first(key, monkeypatch):
    monkeypatch.setenv("ACME_GATEWAY_KEY", "fake-gateway-key")
    ask = {
        "provider_order": ["gw", "jev", "cli"],
        "providers": {
            "jev": {"kind": "jev", "can": ["score"]},
            "cli": {"kind": "command", "command": ["acme-llm", "--json"], "can": ["understand"], "timeout": 5},
            "gw": {**GATEWAY, "api_key_env": "ACME_GATEWAY_KEY"},
            "unused": GATEWAY,
        },
    }
    gw, jev, cli = make_config(ask=ask).ask.providers
    assert (gw.name, gw.kind, gw.can, gw.api_key) == ("gw", "openai", ("understand", "score"), "fake-gateway-key")
    assert (gw.base_url, gw.model) == ("http://127.0.0.1:4000/v1", "acme-default")
    assert (jev.kind, jev.can, jev.api_key, jev.model) == ("jev", ("score",), "fake-typesafe-key", "jev-latest")
    assert (cli.kind, cli.command, cli.can, cli.timeout) == ("command", ("acme-llm", "--json"), ("understand",), 5.0)


@pytest.mark.parametrize(
    ("ask", "message"),
    [
        ({"providers": {"gw": GATEWAY}}, "provider_order"),
        ({"provider_order": ["gw"]}, "gw"),
        ({"provider_order": ["gw", "gw"], "providers": {"gw": GATEWAY}}, "twice"),
        ({"provider_order": "gw", "providers": {"gw": GATEWAY}}, "provider_order"),
        ({"provider_order": ["gw"], "providers": {"gw": {**GATEWAY, "kind": "smoke"}}}, "kind"),
        ({"provider_order": ["gw"], "providers": {"gw": {**GATEWAY, "can": ["write"]}}}, "can"),
        ({"provider_order": ["gw"], "providers": {"gw": {**GATEWAY, "can": []}}}, "can"),
        ({"provider_order": ["gw"], "providers": {"gw": {**GATEWAY, "base_url": "http://10.0.0.5/v1"}}}, "https"),
        ({"provider_order": ["gw"], "providers": {"gw": {"kind": "openai", "model": "m"}}}, "base_url"),
        ({"provider_order": ["gw"], "providers": {"gw": {"kind": "openai", "base_url": "https://llm.test"}}}, "model"),
        ({"provider_order": ["gw"], "providers": {"gw": {**GATEWAY, "command": ["x"]}}}, "command"),
        ({"provider_order": ["c"], "providers": {"c": {"kind": "command", "command": "acme-llm --json"}}}, "command"),
        ({"provider_order": ["c"], "providers": {"c": {"kind": "command", "command": []}}}, "command"),
        ({"provider_order": ["c"], "providers": {"c": {"kind": "command", "command": ["x"], "timeout": 0}}}, "timeout"),
        ({"provider_order": ["c"], "providers": {"c": {"kind": "command", "command": ["x"], "model": "m"}}}, "model"),
        ({"provider_order": ["j"], "providers": {"j": {"kind": "jev", "base_url": "http://typesafe.test"}}}, "https"),
        ({"provider_order": ["a b"], "providers": {"a b": GATEWAY}}, "name"),
    ],
)
def test_a_provider_the_config_does_not_describe_is_refused(ask, message):
    with pytest.raises(ConfigError, match=message):
        make_config(ask=ask)


def test_a_repository_file_cannot_declare_or_order_providers(tmp_path):
    own = tmp_path / ".graylog-mcp.toml"
    own.write_text("", encoding="utf-8")
    for ask in ({"providers": {"gw": GATEWAY}, "provider_order": ["gw"]}, {"provider_order": []}):
        with pytest.raises(ConfigError, match="repository's own config"):
            parse_config({**MAIN, "ask": ask}, source=str(own))
    assert parse_config({**MAIN, "ask": {"language": "vi"}}, source=str(own)).ask.language == "vi"


def test_a_provider_without_its_key_stays_listed():
    ask = {"provider_order": ["gw"], "providers": {"gw": {**GATEWAY, "api_key_env": "ACME_GATEWAY_KEY"}}}
    (gw,) = make_config(ask=ask).ask.providers
    assert (gw.api_key_env, gw.api_key) == ("ACME_GATEWAY_KEY", None)


def test_what_may_be_handed_to_a_provider_is_set_per_instance():
    env = {"url": "https://graylog.test", "token_env": "TEST_GRAYLOG_TOKEN"}
    cfg = parse_config(
        {
            "environments": {"prod": {"provider_data": "none"}},
            "groups": {
                "shop": {"environments": {"prod": dict(env), "dev": dict(env)}},
                "bank": {
                    "provider_data": "question_only",
                    "environments": {"prod": {**env, "provider_data": "question_and_logs"}, "dev": dict(env)},
                },
            },
            "default_instance": "shop/dev",
        },
        source="test",
    )
    said = {name: inst.provider_data for name, inst in cfg.instances.items()}
    assert said == {
        "shop/prod": "none",
        "shop/dev": "question_and_logs",
        "bank/prod": "question_and_logs",
        "bank/dev": "question_only",
    }
    with pytest.raises(ConfigError, match="provider_data"):
        make_config(instances={"main": {"url": "https://graylog.test", "token_env": "T", "provider_data": "all"}})


# ------------------------------------------------------------------ who is asked


class Stub:
    def __init__(self, answers: dict[str, Any] | None = None, error: str | None = None, words: bool = True):
        self.replies, self.error, self.words = answers or {}, error, words
        self.requests = self.bytes_sent = 0
        self.asked: list[tuple[Any, dict[str, Any]]] = []

    async def answers(self, state, questions):
        self.requests += 1
        self.bytes_sent += 10
        self.asked.append((state, questions))
        if self.error:
            raise ProviderError(self.error)
        return self.replies

    async def aclose(self) -> None:
        pass


def _named(name: str, *can: str) -> ProviderConfig:
    return ProviderConfig(name=name, kind="command", can=can or ("understand", "score"), command=("x",))


INTENT = {"intent": {"type": "choice", "instructions": "?", "criteria": {"count": "how many", "search": "lines"}}}
RELATED = {"c0": {"type": "noul", "instructions": "?"}, "c1": {"type": "noul", "instructions": "?"}}
COUNT = {"intent": {"choice": "count", "confidence": 0.9}}


async def test_the_first_provider_able_to_do_the_task_is_asked_alone():
    scorer, first, second = Stub(), Stub(COUNT), Stub(COUNT)
    providers = Providers([(_named("s", "score"), scorer), (_named("a"), first), (_named("b"), second)])
    failed: list[dict[str, str]] = []

    got = await providers.ask(UNDERSTAND, "question_only", "how many", INTENT, failed)

    assert (got.provider, got.answers) == ("a", COUNT)
    assert (scorer.requests, first.requests, second.requests) == (0, 1, 0)
    assert first.asked == [("how many", INTENT)]
    assert failed == []
    assert providers.usage() == {"s": (0, 0), "a": (1, 10), "b": (0, 0)}


@pytest.mark.parametrize(
    ("task", "policy", "asked"),
    [
        (UNDERSTAND, "none", False),
        (UNDERSTAND, "question_only", True),
        (UNDERSTAND, "question_and_logs", True),
        (SCORE, "none", False),
        (SCORE, "question_only", False),
        (SCORE, "question_and_logs", True),
    ],
)
async def test_the_instance_decides_what_a_provider_may_be_handed(task, policy, asked):
    stub = Stub({**COUNT, "c0": {"noul": 0.9}, "c1": {"noul": 0.1}})
    providers = Providers([(_named("a"), stub)])
    assert providers.may_ask(task, policy) is asked
    got = await providers.ask(task, policy, "q", INTENT if task == UNDERSTAND else RELATED, [])
    assert (got is not None) is asked
    assert stub.requests == (1 if asked else 0)


async def test_the_next_provider_is_asked_only_when_the_one_before_failed():
    broken, second = Stub(error="cannot reach acme-llm"), Stub(COUNT)
    providers = Providers([(_named("a"), broken), (_named("b"), second)])
    failed: list[dict[str, str]] = []

    got = await providers.ask(UNDERSTAND, "question_and_logs", "a secret question", INTENT, failed)

    assert got.provider == "b"
    assert failed == [{"provider": "a", "task": "understand", "reason": "cannot reach acme-llm"}]


async def test_nobody_left_to_ask_is_no_answer():
    providers = Providers([(_named("a"), Stub(error="down")), (_named("b"), Stub(error="down too"))])
    failed: list[dict[str, str]] = []
    assert await providers.ask(UNDERSTAND, "question_and_logs", "q", INTENT, failed) is None
    assert [f["provider"] for f in failed] == ["a", "b"]
    assert await Providers([]).ask(UNDERSTAND, "question_and_logs", "q", INTENT, failed) is None
    assert not Providers([]).may_ask(UNDERSTAND, "question_and_logs")


@pytest.mark.parametrize(
    "answer",
    [
        {"choice": "delete", "confidence": 0.99},
        {"choice": ["count"]},
        {"noul": 0.9},
        "count",
        None,
    ],
)
async def test_a_choice_outside_the_list_is_not_believed(answer):
    stub, second = Stub({"intent": answer}), Stub(COUNT)
    providers = Providers([(_named("a"), stub), (_named("b"), second)])
    failed: list[dict[str, str]] = []
    got = await providers.ask(UNDERSTAND, "question_only", "q", INTENT, failed)
    # nothing of the answer could be used: it counts as a failure, and the next one is asked
    assert got.provider == "b"
    assert failed[0]["provider"] == "a" and "delete" not in failed[0]["reason"]


async def test_the_part_of_an_answer_that_can_be_read_is_kept():
    stub = Stub({"intent": {"choice": "count", "confidence": 7}, "c0": {"noul": 0.4}, "c1": {"noul": 7}, "extra": 1})
    providers = Providers([(_named("a"), stub)])
    got = await providers.ask(SCORE, "question_and_logs", "q", {**INTENT, **RELATED}, [])
    assert got.answers == {"intent": {"choice": "count", "confidence": 1.0}, "c0": {"noul": 0.4}}


async def test_a_choice_without_a_confidence_has_none():
    providers = Providers([(_named("a"), Stub({"intent": {"choice": "search"}}))])
    got = await providers.ask(UNDERSTAND, "question_only", "q", INTENT, [])
    assert got.answers == {"intent": {"choice": "search", "confidence": 0.0}}


async def test_words_are_plain_words_or_nothing():
    asked = {"keywords": {"type": "words", "instructions": "?", "max": 3}}
    said = {"keywords": {"words": ["Refund", "level:3 OR *", "a", "time-out", "bank.call", 7, "x" * 50, "late"]}}
    providers = Providers([(_named("a"), Stub(said))])
    got = await providers.ask(UNDERSTAND, "question_only", "q", asked, [])
    assert got.answers == {"keywords": {"words": ["refund", "time-out", "bank.call"]}}


async def test_words_are_asked_only_of_a_provider_that_gives_them():
    asked = {"keywords": {"type": "words", "instructions": "?", "max": 3}}
    choosing, wording = Stub(COUNT, words=False), Stub({"keywords": {"words": ["refund"]}})
    providers = Providers([(_named("a"), choosing), (_named("b"), wording)])
    assert providers.may_ask(UNDERSTAND, "question_only", words=True)
    got = await providers.ask(UNDERSTAND, "question_only", "q", asked, [])
    assert (got.provider, choosing.requests) == ("b", 0)
    assert not Providers([(_named("a"), choosing)]).may_ask(UNDERSTAND, "question_only", words=True)


# ------------------------------------------------------------------ reading a model's text


@pytest.mark.parametrize(
    "text",
    [
        '{"answers": {"a": 1}}',
        '```json\n{"answers": {"a": 1}}\n```',
        '<think>the user wants {"x": 2}</think>\n{"answers": {"a": 1}}',
        'Here is the answer:\n{"answers": {"a": 1}}\nHope this helps.',
        '  {"answers": {"a": 1}} trailing } brace',
    ],
)
def test_the_json_object_of_a_reply_is_found_in_what_surrounds_it(text):
    assert json_object(text) == {"answers": {"a": 1}}


@pytest.mark.parametrize("text", ["", "no json here", "[1, 2]", '{"answers": ', "<think>never closed {}"])
def test_a_reply_without_a_json_object_is_refused(text):
    with pytest.raises(ValueError):
        json_object(text)


# ------------------------------------------------------------------ an OpenAI-compatible endpoint


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr("graylog_mcp.ask.openai.RETRY_SECONDS", 0)


def _gateway(fake: FakeOpenAI, **settings) -> OpenAIClient:
    cfg = ProviderConfig(
        name="gw",
        kind="openai",
        base_url="http://127.0.0.1:4000/v1",
        model="acme-default",
        **{"api_key_env": "ACME_GATEWAY_KEY", "api_key": "fake-gateway-key", **settings},
    )
    return OpenAIClient(cfg, fake.transport)


REPLY = '{"answers": {"intent": {"choice": "count", "confidence": 0.8}}}'


@pytest.mark.parametrize(
    "content",
    [REPLY, f"```json\n{REPLY}\n```", f"<think>count or search?</think>\n{REPLY}", f"The answer is {REPLY}. Done."],
)
async def test_the_endpoint_is_asked_in_one_message_and_its_reply_read(content):
    fake = FakeOpenAI([content])
    client = _gateway(fake)

    got = await client.answers("how many errors", INTENT)

    assert got == {"intent": {"choice": "count", "confidence": 0.8}}
    (body,) = fake.bodies
    assert (body["model"], body["temperature"]) == ("acme-default", 0)
    assert body["response_format"] == {"type": "json_object"}
    system, user = body["messages"]
    assert (system["role"], user["role"]) == ("system", "user")
    assert json.loads(user["content"]) == {"state": "how many errors", "questions": INTENT}
    assert "how many errors" not in system["content"]  # the instructions are fixed text
    assert (client.requests, client.bytes_sent) == (1, len(fake.sent[0]))
    await client.aclose()


async def test_answers_without_the_wrapper_and_a_choice_without_a_confidence_are_read():
    client = _gateway(FakeOpenAI(['{"intent": {"choice": "search"}, "c0": {"noul": 0.2}}']))
    got = await client.answers("q", {**INTENT, **RELATED})
    assert got == {"intent": {"choice": "search", "confidence": 0.7}, "c0": {"noul": 0.2}}


async def test_an_endpoint_that_refuses_json_mode_is_asked_again_without_it():
    fake = FakeOpenAI([400, REPLY, REPLY])
    client = _gateway(fake)
    assert await client.answers("q", INTENT)
    assert "response_format" in fake.bodies[0] and "response_format" not in fake.bodies[1]
    await client.answers("q", INTENT)
    assert "response_format" not in fake.bodies[2]  # remembered: not refused again on every question
    assert client.requests == 3


@pytest.mark.parametrize(
    ("replies", "message", "sent"),
    [
        ([500, 500], "HTTP 500", 2),
        ([503, REPLY], None, 2),
        ([401], "ACME_GATEWAY_KEY", 1),
        ([404], "HTTP 404", 1),
        (["I cannot answer that."], "cannot read", 1),
        ([400, 400], "HTTP 400", 2),
    ],
)
async def test_an_endpoint_that_fails_is_a_provider_error_without_what_was_sent(replies, message, sent):
    fake = FakeOpenAI(replies)
    client = _gateway(fake)
    if message is None:
        assert await client.answers("a secret question", INTENT)
    else:
        with pytest.raises(ProviderError, match=message) as failure:
            await client.answers("a secret question", INTENT)
        assert "secret" not in str(failure.value)
    assert len(fake.bodies) == sent


async def test_an_endpoint_that_cannot_be_reached_is_tried_twice():
    calls = []

    def refuse(request):
        calls.append(request)
        raise httpx.ConnectError("no route")

    cfg = ProviderConfig(name="gw", kind="openai", base_url="https://llm.test/v1", model="m")
    client = OpenAIClient(cfg, httpx.MockTransport(refuse))
    with pytest.raises(ProviderError, match="cannot reach gw"):
        await client.answers("q", INTENT)
    assert len(calls) == 2


async def test_an_endpoint_without_a_key_gets_no_authorization_header():
    fake = FakeOpenAI([REPLY], key=None)
    assert await _gateway(fake, api_key_env="", api_key=None).answers("q", INTENT)


async def test_a_key_that_was_never_saved_is_said_before_anything_is_sent():
    fake = FakeOpenAI([REPLY])
    with pytest.raises(ProviderError, match="no API key in ACME_GATEWAY_KEY"):
        await _gateway(fake, api_key=None).answers("q", INTENT)
    assert fake.bodies == []


# ------------------------------------------------------------------ a command on this machine

FAKE_COMMAND = str(Path(__file__).parent / "fake_command.py")


def _command(tmp_path, mode: str = "", timeout: float = 20) -> tuple[CommandClient, Path]:
    record = tmp_path / "asked.jsonl"
    command = (sys.executable, FAKE_COMMAND, str(record), *([mode] if mode else []))
    return CommandClient(ProviderConfig(name="cli", kind="command", command=command, timeout=timeout)), record


@pytest.mark.parametrize("mode", ["", "prose"])
async def test_a_command_reads_the_questions_on_stdin_and_answers_on_stdout(tmp_path, mode):
    client, record = _command(tmp_path, mode)

    got = await client.answers({"request": "refund failed"}, {**INTENT, **RELATED})

    assert got["intent"] == {"choice": "count", "confidence": 0.9} and got["c0"] == {"noul": 0.9}
    asked = json.loads(record.read_text(encoding="utf-8"))
    assert asked == {"version": 1, "state": {"request": "refund failed"}, "questions": {**INTENT, **RELATED}}
    assert client.requests == 1 and client.bytes_sent > 0


async def test_a_command_that_hangs_is_stopped_at_its_time_limit(tmp_path):
    client, _ = _command(tmp_path, "hang", timeout=1)
    began = time.monotonic()
    with pytest.raises(ProviderError, match="did not answer within 1s"):
        await client.answers("q", INTENT)
    assert time.monotonic() - began < 5
    assert client.last_exit is not None  # the process was reaped, not left behind


@pytest.mark.parametrize(
    ("mode", "message"),
    [("fail", "exited with code 3"), ("flood", "more than"), ("missing", "cannot run")],
)
async def test_a_command_that_fails_is_a_provider_error_without_its_output(tmp_path, mode, message):
    client, _ = _command(tmp_path, mode)
    if mode == "missing":
        cfg = ProviderConfig(name="cli", kind="command", command=("acme-llm-that-is-not-installed",))
        client = CommandClient(cfg)
    with pytest.raises(ProviderError, match=message) as failure:
        await client.answers("a secret question", INTENT)
    assert "secret" not in str(failure.value)
    if mode == "flood":  # stopped while it was still writing, and reaped
        assert client.last_exit is not None


def test_each_kind_of_provider_gets_its_own_client(tmp_path):
    kinds = [
        ProviderConfig(name="j", kind="jev", base_url="https://api.typesafe.ai", model="jev-latest", api_key="fake-k"),
        ProviderConfig(name="o", kind="openai", base_url="https://llm.test/v1", model="m"),
        ProviderConfig(name="c", kind="command", command=("acme-llm",)),
    ]
    made = Providers.create(kinds)
    assert [type(client).__name__ for _, client in made.order] == ["JevClient", "OpenAIClient", "CommandClient"]
    assert [client.words for _, client in made.order] == [False, True, True]
