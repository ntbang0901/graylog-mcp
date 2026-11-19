"""``ask`` by its rules alone, and what it asks of a provider only for the rest."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from graylog_mcp.ask.pipeline import ask
from graylog_mcp.config import parse_config
from graylog_mcp.server import build_server
from graylog_mcp.shaping import dumps
from graylog_mcp.tools import App
from tests.conftest import make_config
from tests.fake_graylog import PAY, FakeGraylog
from tests.fake_jev import FakeJev
from tests.fake_openai import FakeOpenAI
from tests.test_tools_contract import LEAKS
from tests.test_trace import _add

PAYMENT = {"aliases": ["thanh toán"], "query": "service:payment", "description": "payment service"}
TERMS = {"terms": {"payment": PAYMENT}}
GATEWAY = {"kind": "openai", "base_url": "http://127.0.0.1:4000/v1", "model": "acme-default"}
FAKE_COMMAND = str(Path(__file__).parent / "fake_command.py")


def server(name: str, **settings) -> dict:
    return {"url": f"https://{name}.test", "token_env": "TEST_GRAYLOG_TOKEN", **settings}


@pytest.fixture(autouse=True)
def no_key(monkeypatch):
    """A developer's own key must not decide these tests; the ones about a provider set theirs."""
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setattr("graylog_mcp.ask.openai.RETRY_SECONDS", 0)


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "fake-typesafe-key")


def routed(**fakes) -> httpx.MockTransport:
    """One transport for every HTTP provider: ``jev`` answers for TypeSafe, ``gw`` for the gateway."""

    def handle(request: httpx.Request) -> httpx.Response:
        fake = fakes["jev" if request.url.host == "api.typesafe.ai" else "gw"]
        return fake.handle(request)

    return httpx.MockTransport(handle)


@pytest.fixture
def make_app():
    def factory(dataset: str = "incident", providers: httpx.MockTransport | None = None, **overrides):
        fake = FakeGraylog(dataset=dataset)
        if dataset == "incident":  # the flow crosses streams: payment logs to its own
            for m in fake.messages:
                if m["service"] == "payment":
                    m["streams"] = [PAY]
        app = App.create(make_config(**overrides), transport=fake.transport, provider_transport=providers)
        return app, fake

    return factory


def searches(fake: FakeGraylog) -> list:
    return [r for r in fake.requests if r.method == "POST"]


# ------------------------------------------------------------------ without any provider


async def test_the_acceptance_question_is_answered_without_any_provider(make_app, key):
    """A key is saved, the user turned every provider off: the rules alone must read the whole question."""
    jev = FakeJev()
    app, fake = make_app(
        "basic",
        routed(jev=jev),
        ask={"provider_order": []},
        instances={"prod": server("prod"), "dev": server("dev")},
        glossary=TERMS,
    )
    _add(fake, 300, service="payment", http_status=500, level=3, message="bank call failed")
    _add(fake, 200, service="payment", http_status=422, message="amount refused")

    out = await ask(app, "thanh toán lỗi trong 15 phút qua ở prod")

    understood = out["understood"]
    assert understood["instance"] == "prod" and understood["sources"]["instance"] == "rules"
    assert understood["time"]["words"] == "trong 15 phút qua"
    first, last = (datetime.fromisoformat(understood["time"][k]) for k in ("from", "to"))
    assert (last - first).total_seconds() == 900
    assert understood["topics"] == [{"term": "payment", "matched": "thanh toán", "query": "service:payment"}]
    assert '"thanh toán" is understood as payment service, filtered by service:payment.' in out["summary"]
    assert understood["errors"] == "both" and "technical and business errors" in out["summary"]
    assert out["by_kind"]["business"] == 1 and out["by_kind"]["technical"] > 1  # each kind counted on its own
    assert out["ran"].endswith("AND (service:payment)")
    assert out["cost"]["provider_requests"] == 0 and "providers" not in out["cost"]
    assert jev.bodies == [] and app.providers.order == []


async def test_a_question_that_names_what_it_asks_is_answered_by_rule(make_app):
    app, _ = make_app()
    out = await ask(app, "how many errors in the last hour?")
    assert out["result"]["count"] > 0
    assert out["understood"]["sources"] == {
        "intent": "rules",
        "instance": "default",
        "time": "rules",
        "errors": "rules",
    }
    assert "guessed" not in out and "clarify" not in out
    assert out["cost"]["provider_requests"] == 0


async def test_a_described_error_is_matched_by_its_words_and_said_to_be_a_guess(make_app):
    app, _ = make_app()

    out = await ask(app, "bank call failed in the last hour")

    assert out["understood"]["intent"] == "follow_flow" and out["understood"]["sources"]["intent"] == "guess"
    assert out["guessed"] == {"intent": "follow_flow", "others": ["error_summary", "root_cause"]}
    assert "what are the main errors" in out["summary"] and "root cause" in out["summary"]
    assert out["scored_by"] == "rules"
    assert "bank call failed" in out["matches"][0]["sample"] and out["matches"][0]["score"] == 1.0
    assert out["flow"]["first_error"]["service"] == "payment"


async def test_a_word_for_a_kind_of_error_also_describes_the_error(make_app):
    """ "timeout" says the error is technical, and it is the word its message holds: both readings count."""
    app, _ = make_app("basic")
    out = await ask(app, "timeout in the last hour")
    assert out["understood"]["errors"] == "technical" and out["scored_by"] == "rules"
    assert out["matches"] and all("timeout" in m["sample"] and m["score"] == 1.0 for m in out["matches"])
    # the bare word "error" tells nothing apart
    assert "No word of the question" in (await ask(app, "errors in the last hour"))["summary"]


async def test_a_kind_without_errors_costs_one_count_not_a_search_for_why(make_app):
    app, _ = make_app("basic", instances={"main": server("graylog", business_error_query="http_status:499")})
    out = await ask(app, "how many errors in the last hour")
    assert out["result"]["by_kind"]["business"] == 0 and out["result"]["by_kind"]["technical"] > 0
    # the version and the fields of a first call, the count itself, and one request for each kind
    assert out["cost"]["graylog_requests"] <= 6

    out = await ask(app, "errors in the last hour")  # followed, with the split as an extra
    assert out["by_kind"]["business"] == 0 and out["cost"]["graylog_requests"] <= 6


@pytest.mark.parametrize("question", ["the printer failed in the last hour", "thanh toan bi loi trong 1 gio qua"])
async def test_words_no_error_holds_do_not_hide_the_errors_there_are(make_app, question):
    """By rule a word that matches nothing may only be another language than the logs': it is no proof that
    nothing is wrong."""
    app, _ = make_app()
    out = await ask(app, question)
    assert out["scored_by"] == "rules" and out["matches"] and "flow" not in out
    assert all(m["score"] == 0 for m in out["matches"])
    assert "None of the errors in this period holds a word of your description" in out["summary"]


async def test_words_that_only_carry_the_sentence_are_not_looked_for(make_app):
    app, fake = make_app("basic")
    _add(fake, 60, level=3, service="gateway", message="uptime check failed since restart, please look")
    out = await ask(app, "could you please look at what failed with the bank since then, in the last hour")
    # "please", "look", "since", "could", "what" carry the sentence; "bank" describes the error
    assert out["matches"] and all("bank" in m["sample"].lower() for m in out["matches"])


async def test_a_description_with_no_word_to_match_lists_the_most_frequent_errors(make_app):
    app, _ = make_app(glossary=TERMS)
    out = await ask(app, "thanh toán lỗi hôm nay")
    assert out["scored_by"] == "rules" and out["matches"] and "flow" not in out
    assert all(m["score"] == 0 for m in out["matches"])
    assert "No word of the question tells the errors apart" in out["summary"]


async def test_words_of_the_time_the_place_and_the_intent_are_not_what_is_matched(make_app):
    app, _ = make_app(instances={"prod": server("prod")})
    out = await ask(app, "where does checkout go wrong on prod since this morning")
    # "prod", "since", "morning", "wrong", "where", "does": none of them describes the error; "checkout" does
    assert out["understood"]["sources"]["intent"] == "rules" and out["scored_by"] == "rules"
    assert all("checkout" in m["sample"].lower() for m in out["matches"])


async def test_a_question_nothing_can_be_read_from_is_asked_back_with_readings(make_app):
    app, fake = make_app()
    out = await ask(app, "xin chào")
    assert "matches" not in out and "result" not in out
    assert [r["intent"] for r in out["readings"]] == ["follow_flow", "error_summary", "search"]
    assert all(r["ask"] in out["clarify"] for r in out["readings"])
    assert searches(fake) == []


async def test_an_environment_of_several_groups_is_asked_back_with_the_choices(make_app):
    env = {"url": "https://graylog.test", "token_env": "TEST_GRAYLOG_TOKEN"}
    groups = {name: {"environments": {"prod": dict(env), "dev": dict(env)}} for name in ("shop", "bank")}
    fake = FakeGraylog(dataset="incident")
    app = App.create(parse_config({"groups": groups, "default_instance": "shop/dev"}, source="test"), fake.transport)

    out = await ask(app, "how many errors on prod today")

    assert out["options"] == ["bank/prod", "shop/prod"]
    assert "bank/prod" in out["clarify"] and "shop/prod" in out["clarify"]
    assert "result" not in out and searches(fake) == []
    assert (await ask(app, "how many errors on bank prod today"))["understood"]["instance"] == "bank/prod"


async def test_an_answer_scored_by_rule_stays_under_the_size_cap(make_app):
    app, fake = make_app("basic", ask={"send_chars": 2000, "max_candidates": 30})
    fake.messages = []
    for i in range(40):
        word = "".join(chr(97 + int(d)) for d in f"{i:02d}")
        _add(fake, 600 - i, level=3, service="payment", message=f"ledger {word} " + "y" * 2500)
    out = await ask(app, "ledger failure in the last hour")
    assert len(dumps(out)) <= app.config.limits.max_output_chars
    assert out["matches"] and out["matches_omitted"] > 0


async def test_ask_is_declared_without_any_key(make_app):
    app, _ = make_app()
    assert "ask" in {t.name for t in await build_server(app).list_tools()}


# ------------------------------------------------------------------ with a provider


async def test_a_provider_is_not_asked_what_the_rules_read_for_sure(make_app, key):
    jev = FakeJev()
    app, _ = make_app(providers=routed(jev=jev))
    out = await ask(app, "how many errors in the last hour?")
    assert out["result"]["count"] > 0 and jev.bodies == []
    assert out["cost"]["provider_requests"] == 0


async def test_a_guess_is_checked_with_the_provider_and_only_the_open_parts_are_asked(make_app, key):
    jev = FakeJev(intent="error_summary")
    app, _ = make_app(providers=routed(jev=jev))

    out = await ask(app, "bank call failed")

    assert set(jev.bodies[0]["questions"]) == {"intent", "time"}  # the kind of error was read: "failed"
    assert out["understood"]["intent"] == "error_summary" and out["understood"]["sources"]["intent"] == "jev"
    assert "guessed" not in out and out["result"]["groups"]
    assert out["cost"]["provider_requests"] == 1
    assert out["cost"]["providers"] == {"jev": {"requests": 1, "bytes_sent": len(json.dumps(jev.bodies[0]))}}


async def test_a_time_the_rules_could_not_place_is_asked_of_the_provider(make_app, key):
    jev = FakeJev(time="last_7_days")
    app, _ = make_app(providers=routed(jev=jev))
    out = await ask(app, "how many errors tuần rồi")
    assert set(jev.bodies[0]["questions"]) == {"time"}
    assert out["understood"]["time"] == {"choice": "last_7_days"} and out["understood"]["sources"]["time"] == "jev"


async def test_an_unsure_provider_is_asked_back_with_its_reading_first(make_app, key):
    app, fake = make_app(providers=routed(jev=FakeJev(intent="compare", confidence=0.3)))
    out = await ask(app, "bank call failed")
    assert [r["intent"] for r in out["readings"]] == ["compare", "follow_flow", "error_summary"]
    assert "clarify" in out and searches(fake) == []


async def test_a_provider_is_not_asked_for_an_environment_the_question_does_not_name(make_app, key):
    """It would have to pick one: the asker then gets another environment's numbers."""
    jev = FakeJev(intent="count", instance="prod")
    app, _ = make_app(providers=routed(jev=jev), instances={"prod": server("prod"), "dev": server("dev")})
    out = await ask(app, "bank call failed")
    assert set(jev.bodies[0]["questions"]) == {"intent", "time"}
    assert (out["understood"]["instance"], out["understood"]["sources"]["instance"]) == ("main", "default")


def two_groups(fake: FakeGraylog, providers: httpx.MockTransport) -> App:
    env = {"url": "https://graylog.test", "token_env": "TEST_GRAYLOG_TOKEN"}
    groups = {name: {"environments": {"prod": dict(env), "dev": dict(env)}} for name in ("shop", "bank")}
    config = parse_config({"groups": groups, "default_instance": "shop/dev"}, source="test")
    return App.create(config, fake.transport, provider_transport=providers)


async def test_a_provider_chooses_only_among_the_environments_the_words_fit(make_app, key):
    fake = FakeGraylog(dataset="incident")
    jev = FakeJev(intent="count", instance="bank/prod")
    out = await ask(two_groups(fake, routed(jev=jev)), "errors on prod")  # a guess, and "prod" is in two groups
    assert set(jev.bodies[0]["questions"]["instance"]["criteria"]) == {"bank/prod", "shop/prod"}
    assert (out["understood"]["instance"], out["understood"]["sources"]["instance"]) == ("bank/prod", "jev")

    for elsewhere in (FakeJev(instance="shop/dev"), FakeJev(instance="bank/prod", instance_confidence=0.3)):
        out = await ask(two_groups(fake, routed(jev=elsewhere)), "errors on prod")
        # an environment the question did not name, or an unsure pick, is not taken: the asker chooses
        assert out["options"] == ["bank/prod", "shop/prod"] and "result" not in out and "matches" not in out


async def test_an_unsure_period_is_asked_back_as_a_period(make_app, key):
    app, fake = make_app(providers=routed(jev=FakeJev(time="last_7_days", confidence=0.3)))
    out = await ask(app, "how many errors tuần rồi")
    assert "result" not in out and "readings" not in out and searches(fake) == []
    assert "which period" in out["clarify"] and "tuần rồi" not in out["clarify"]  # the rules could not point at it


async def test_a_period_named_but_not_read_is_said_when_nobody_can_be_asked(make_app):
    app, _ = make_app()
    out = await ask(app, "how many errors tuần rồi")
    assert out["understood"]["sources"]["time"] == "default" and out["result"]["count"] >= 0
    assert "seems to name a period" in out["summary"] and "last 24 hours" in out["summary"]
    assert "seems to name a period" not in (await ask(app, "how many errors"))["summary"]


async def test_two_questions_at_once_each_count_what_they_sent(make_app, key):
    jev = FakeJev(intent="count")
    both_asked = asyncio.Event()

    async def held(request: httpx.Request) -> httpx.Response:
        """Answers only once both questions have sent theirs: each is still being answered when the other asks."""
        reply = jev.handle(request)
        if len(jev.bodies) == 2:
            both_asked.set()
        await both_asked.wait()
        return reply

    app, _ = make_app(providers=httpx.MockTransport(held))
    one, two, none = await asyncio.gather(
        ask(app, "bank call failed"), ask(app, "bank call failed"), ask(app, "how many errors in the last hour")
    )
    assert one["cost"]["provider_requests"] == two["cost"]["provider_requests"] == 1
    assert one["cost"]["providers"]["jev"]["bytes_sent"] == len(json.dumps(jev.bodies[0]))
    assert none["cost"]["provider_requests"] == 0 and "providers" not in none["cost"]


async def test_an_environment_the_rules_read_is_not_asked_of_the_provider(make_app, key):
    jev = FakeJev(intent="count")
    app, _ = make_app(providers=routed(jev=jev), instances={"prod": server("prod"), "dev": server("dev")})
    out = await ask(app, "bank call failed on prod")
    assert set(jev.bodies[0]["questions"]) == {"intent", "time"}
    assert out["understood"]["sources"]["instance"] == "rules"


# ------------------------------------------------------------------ what an instance lets a provider have


async def test_an_instance_that_hands_over_nothing_is_answered_by_rule(make_app, key):
    jev = FakeJev()
    app, _ = make_app(providers=routed(jev=jev), instances={"main": server("graylog", provider_data="none")})
    out = await ask(app, "bank call failed in the last hour")
    assert jev.bodies == [] and out["scored_by"] == "rules" and out["guessed"]["intent"] == "follow_flow"
    assert out["cost"]["provider_requests"] == 0


async def test_the_question_alone_may_be_handed_over_and_the_logs_kept(make_app):
    gw = FakeOpenAI(chooser=FakeJev(intent="follow_flow", keywords=("bank", "level:3")), key=None)
    app, _ = make_app(
        providers=routed(gw=gw),
        ask={"provider_order": ["gw"], "providers": {"gw": GATEWAY}},
        instances={"main": server("graylog", provider_data="question_only")},
    )

    out = await ask(app, "ngân hàng không gọi được trong 1 giờ qua")

    asked = [json.loads(body["messages"][-1]["content"]) for body in gw.bodies]
    assert [set(a["questions"]) for a in asked] == [{"intent", "errors_only"}, {"keywords"}]
    assert all(isinstance(a["state"], str) for a in asked)  # the question, never a candidate
    assert out["scored_by"] == "rules" and out["understood"]["sources"]["intent"] == "gw"
    assert "bank call failed" in out["matches"][0]["sample"]
    assert out["cost"]["providers"]["gw"]["requests"] == 2


async def test_before_the_environment_is_known_the_strictest_instance_decides(make_app, key):
    jev = FakeJev()
    instances = {"prod": server("prod", provider_data="none"), "dev": server("dev")}
    app, fake = make_app(providers=routed(jev=jev), instances=instances)

    out = await ask(app, "bank call failed in the last hour")  # a guess: a provider would be asked

    assert jev.bodies == [] and searches(fake) == []
    assert out["options"] == ["dev", "main", "prod"] and "clarify" in out
    # named, the environment's own setting decides
    assert (await ask(app, "bank call failed on dev in the last hour"))["understood"]["sources"]["intent"] == "jev"
    assert len(jev.bodies) == 2  # the intent, then the scores
    bodies = len(jev.bodies)
    out = await ask(app, "bank call failed on prod in the last hour")
    assert len(jev.bodies) == bodies and out["scored_by"] == "rules"
    # and a question the rules read needs nobody
    assert (await ask(app, "how many errors in the last hour"))["result"]["count"] >= 0


@pytest.mark.parametrize("allowed", ["question_and_logs", "question_only"])
async def test_nothing_is_handed_over_for_scoring_either_before_the_environment_is_known(make_app, key, allowed):
    """The default instance is where the search runs, not what the asker said: the question may be about the
    instance that forbids it."""
    jev, gw = FakeJev(relevant=("bank call failed",)), FakeOpenAI(key=None)
    instances = {"main": server("graylog", provider_data=allowed), "prod": server("prod", provider_data="none")}
    declared = {"provider_order": ["gw", "jev"], "providers": {"gw": GATEWAY, "jev": {"kind": "jev"}}}
    app, _ = make_app(providers=routed(jev=jev, gw=gw), instances=instances, ask=declared)

    out = await ask(app, "where does the bank call go wrong in the last hour")  # what is asked is read for sure

    assert jev.bodies == [] and gw.bodies == []
    assert out["scored_by"] == "rules" and out["understood"]["sources"]["instance"] == "default"
    # named, the instance's own setting decides again
    await ask(app, "where does the bank call go wrong on main in the last hour")
    assert len(gw.bodies) == 1 and ("candidates" in gw.sent[0].decode()) is (allowed == "question_and_logs")


# ------------------------------------------------------------------ a provider that fails


async def test_the_next_provider_answers_when_the_first_fails(make_app, key):
    gw, jev = FakeOpenAI([500, 500], key=None), FakeJev(intent="count")
    ask_cfg = {"provider_order": ["gw", "jev"], "providers": {"gw": GATEWAY, "jev": {"kind": "jev"}}}
    app, _ = make_app(providers=routed(gw=gw, jev=jev), ask=ask_cfg)

    out = await ask(app, "bank call failed")

    assert out["understood"]["sources"]["intent"] == "jev" and out["result"]["count"] > 0
    assert out["provider_failures"] == [
        {"provider": "gw", "task": "understand", "reason": "gw answered HTTP 500; ask could not use it"}
    ]
    assert "gw" in out["summary"] and "HTTP 500" in out["summary"]
    assert out["cost"]["providers"] == {
        "gw": {"requests": 2, "bytes_sent": sum(map(len, gw.sent))},
        "jev": {"requests": 1, "bytes_sent": len(json.dumps(jev.bodies[0]))},
    }


async def test_the_rules_answer_when_every_provider_fails(make_app, key):
    app, _ = make_app(providers=routed(jev=FakeJev(status=401)))
    out = await ask(app, "bank call failed in the last hour")
    assert out["guessed"]["intent"] == "follow_flow" and out["scored_by"] == "rules"
    assert "bank call failed" in out["matches"][0]["sample"]
    assert [f["task"] for f in out["provider_failures"]] == ["understand"]
    assert "refused the API key" in out["summary"]


async def test_a_scorer_that_fails_leaves_the_scoring_to_the_rules(make_app, key):
    class Flaky(FakeJev):
        def handle(self, request):
            self.status = 200 if "intent" in json.loads(request.content)["questions"] else 503
            return super().handle(request)

    app, _ = make_app(providers=routed(jev=Flaky()))
    out = await ask(app, "bank call failed in the last hour")
    assert out["scored_by"] == "rules" and "bank call failed" in out["matches"][0]["sample"]
    assert [f["task"] for f in out["provider_failures"]] == ["score"]


# ------------------------------------------------------------------ what leaves the machine


@pytest.mark.parametrize("kind", ["jev", "openai", "command"])
async def test_nothing_unredacted_reaches_a_provider_of_any_kind(make_app, monkeypatch, tmp_path, kind):
    record = tmp_path / "asked.jsonl"
    jev, gw = FakeJev(relevant=("calling bank api",)), FakeOpenAI(key=None)
    declared = {
        "jev": {"kind": "jev"},
        "openai": GATEWAY,
        "command": {"kind": "command", "command": [sys.executable, FAKE_COMMAND, str(record)]},
    }
    monkeypatch.setenv("TYPESAFE_API_KEY", "fake-typesafe-key")
    app, fake = make_app(
        "basic", routed(jev=jev, gw=gw), ask={"provider_order": ["p"], "providers": {"p": declared[kind]}}
    )
    assert any(leak in json.dumps(fake.messages) for leak in LEAKS)  # the data really holds them

    # the question carries what a person may paste into it, the candidates what the logs hold
    out = await ask(app, "bank api trouble for alice@example.com, card 4111 1111 1111 1111")

    sent = {
        "jev": lambda: json.dumps(jev.bodies),
        "openai": lambda: b"".join(gw.sent).decode(),
        "command": lambda: record.read_text(encoding="utf-8"),
    }[kind]()
    assert "candidates" in sent  # the error groups went out: the test looks at the path that carries log text
    assert all(leak not in sent for leak in LEAKS)
    assert out["cost"]["providers"]["p"]["bytes_sent"] > 0
