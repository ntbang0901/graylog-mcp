from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import ANY

import pytest

from graylog_mcp.ask.pipeline import ask
from graylog_mcp.ask.understand import time_args
from graylog_mcp.config import ConfigError, parse_config
from graylog_mcp.server import build_server
from graylog_mcp.shaping import dumps
from graylog_mcp.tools import App, ToolInputError
from tests.conftest import make_config
from tests.fake_graylog import PAY, FakeGraylog
from tests.fake_jev import FakeJev
from tests.test_tools_contract import LEAKS, assert_read_only
from tests.test_trace import _add


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "fake-typesafe-key")


@pytest.fixture
def make_ask_app(key):
    apps = []

    def factory(jev: FakeJev, dataset: str = "incident", **overrides):
        fake = FakeGraylog(dataset=dataset)
        if dataset == "incident":  # the flow crosses streams: payment logs to its own
            for m in fake.messages:
                if m["service"] == "payment":
                    m["streams"] = [PAY]
        app = App.create(make_config(**overrides), transport=fake.transport, jev_transport=jev.transport)
        apps.append(app)
        return app, fake

    return factory


async def test_follows_the_flow_of_the_described_error(make_ask_app):
    jev = FakeJev(relevant=("bank call failed",))
    app, fake = make_ask_app(jev)

    out = await ask(app, "why did checkout fail with a bank connection error in the last hour?")

    assert out["understood"] == {
        "intent": "follow_flow",
        "instance": "main",
        "time": {"from": ANY, "to": ANY, "words": "in the last hour"},
        "errors": "both",
        "sources": {"intent": "jev", "instance": "default", "time": "rules", "errors": "rules"},
    }
    assert out["summary"].startswith('Understood: main, "in the last hour" (')
    assert "technical and business errors." in out["summary"].splitlines()[0]
    (match,) = out["matches"]
    assert "bank call failed" in match["sample"] and match["score"] >= 0.9
    services = {s["service"] for s in out["flow"]["services"]}
    assert {"gateway", "payment"} <= services
    assert out["flow"]["first_error"]["service"] == "payment"
    assert "gateway" in out["summary"] and "payment" in out["summary"]
    assert out["cost"]["jev_requests"] == 2
    assert out["cost"]["graylog_requests"] <= 12  # the candidates (count, aggregation, one read), then the trace
    assert_read_only(fake)


async def test_lookup_of_traces_is_one_query_not_one_per_trace(make_ask_app):
    app, fake = make_ask_app(FakeJev(relevant=("bank call failed",)))
    await ask(app, "bank connection error")
    searches = [r for r in fake.requests if r.method == "POST"]
    assert len(searches) <= 4  # candidates, traces, the timeline of one trace (+ one for the full-text retry)


async def test_asks_back_instead_of_guessing(make_ask_app):
    jev = FakeJev(confidence=0.3)
    app, fake = make_ask_app(jev)

    out = await ask(app, "Is the system ok?")

    assert "clarify" in out and "matches" not in out
    assert out["cost"] == {"graylog_requests": 0, "jev_requests": 1}
    assert fake.requests == []


async def test_says_so_when_no_error_matches(make_ask_app):
    app, _ = make_ask_app(FakeJev(relevant=("nothing like this",)))

    out = await ask(app, "the printer is on fire")

    assert out["matches"] == [] and "flow" not in out
    assert "No error in this period matches" in out["summary"]


async def test_lists_close_matches_instead_of_picking(make_ask_app):
    app, _ = make_ask_app(FakeJev(relevant=("bank call failed", "502")))

    out = await ask(app, "checkout error")

    assert len(out["matches"]) == 2 and "flow" not in out
    assert "Several errors match" in out["summary"]


async def test_error_without_trace_id_gets_a_hint(make_ask_app):
    app, _ = make_ask_app(FakeJev(relevant=("deadlock",), time="last_7_days"))

    out = await ask(app, "database deadlock in the past week")

    assert "deadlock" in out["matches"][0]["sample"]
    assert "no trace id" in out["hint"] and "flow" not in out


async def test_other_intents_use_the_existing_tools(make_ask_app):
    app, _ = make_ask_app(FakeJev(intent="count"))
    out = await ask(app, "how many errors?")
    assert out["result"]["count"] > 0 and out["cost"]["jev_requests"] == 1

    app, _ = make_ask_app(FakeJev(intent="error_summary"))
    assert (await ask(app, "what are the main errors?"))["result"]["groups"]

    app, _ = make_ask_app(FakeJev(intent="root_cause"))
    assert "result" in await ask(app, "what is breaking?")


async def test_trace_code_uses_the_configured_patterns(make_ask_app):
    app, _ = make_ask_app(FakeJev(intent="trace_code"), ask={"code_patterns": [r"co-\d+"]})
    out = await ask(app, "what happened to order co-12?")
    assert out["codes"] == ["co-12"] and out["traces"][0]["trace_id"] == "co-12"

    app, _ = make_ask_app(FakeJev(intent="trace_code"))
    assert "code_patterns" in (await ask(app, "what happened to order co-12?"))["hint"]


async def test_vietnamese_summary(make_ask_app):
    app, _ = make_ask_app(FakeJev(relevant=("nothing",)), ask={"language": "vi"})
    out = await ask(app, "lỗi thanh toán")
    assert out["summary"].startswith("Đã hiểu: main, 1 giờ qua, lỗi kỹ thuật và nghiệp vụ.")
    assert out["understood"]["time"] == {"choice": "last_hour"} and out["understood"]["sources"]["time"] == "jev"


async def test_nothing_unredacted_is_sent_to_typesafe(make_ask_app):
    jev = FakeJev(relevant=("calling bank api",))
    app, fake = make_ask_app(jev, dataset="basic")
    assert any(leak in json.dumps(fake.messages) for leak in LEAKS)  # the data really holds them

    await ask(app, "bank api error", instance=None)

    sent = json.dumps(jev.bodies)
    assert jev.bodies and all(leak not in sent for leak in LEAKS)


async def test_typesafe_errors_are_explained(make_ask_app):
    app, _ = make_ask_app(FakeJev(status=401))
    with pytest.raises(Exception, match="refused the API key"):
        await ask(app, "anything")


async def test_empty_question_and_missing_key(make_ask_app, monkeypatch):
    app, _ = make_ask_app(FakeJev())
    with pytest.raises(ToolInputError):
        await ask(app, "   ")
    monkeypatch.delenv("TYPESAFE_API_KEY")
    bare = App.create(make_config())
    assert bare.jev is None
    with pytest.raises(ToolInputError, match="ask is off"):
        await ask(bare, "hello")


async def test_tool_is_registered_only_with_a_key(make_ask_app, make_app, monkeypatch):
    app, _ = make_ask_app(FakeJev())
    assert "ask" in {t.name for t in await build_server(app).list_tools()}
    monkeypatch.delenv("TYPESAFE_API_KEY")
    bare, _ = make_app()
    assert "ask" not in {t.name for t in await build_server(bare).list_tools()}


def test_time_choices_become_tool_arguments():
    tz = make_config().instance(None).tz
    now = datetime(2026, 11, 6, 3, 0, tzinfo=UTC)  # 10:00 in Ho Chi Minh City
    assert time_args("last_hour", tz, now) == {"range": "1h"}
    assert time_args("today", tz, now)["from_time"].startswith("2026-11-06T00:00:00+07:00")
    assert time_args("yesterday", tz, now)["to_time"].startswith("2026-11-06T00:00:00+07:00")
    assert time_args("unspecified", tz, now) == {"range": "24h"}


def test_ask_config_is_validated(key):
    base = {"instances": {"main": {"url": "https://graylog.test", "token_env": "TEST_GRAYLOG_TOKEN"}}}
    cfg = parse_config({**base, "ask": {"language": "vi", "relevance": 0.7}}, source="test").ask
    assert cfg.enabled and cfg.language == "vi" and cfg.relevance == 0.7
    assert "fake-typesafe-key" not in repr(cfg)
    for bad in ({"language": "fr"}, {"base_url": "http://x.test"}, {"relevance": 2}, {"code_patterns": ["("]},
                {"api_key": "x"}, {"api_key_env": "fake-typesafe-key"}, {"base_url": ""}, {"model": ""}):  # fmt: skip
        with pytest.raises(ConfigError):
            parse_config({**base, "ask": bad}, source="test")


def test_a_repository_cannot_choose_where_the_key_goes(key, tmp_path):
    from graylog_mcp.config import load_config

    repo = tmp_path / "acme-app"
    (repo / ".git").mkdir(parents=True)
    body = '[instances.main]\nurl = "https://graylog.test"\ntoken_env = "TEST_GRAYLOG_TOKEN"\n'
    for line in ('base_url = "https://evil.test"', 'api_key_env = "TEST_GRAYLOG_TOKEN"'):
        (repo / ".graylog-mcp.toml").write_text(f"{body}[ask]\n{line}\n")
        with pytest.raises(ConfigError, match="not allowed in a repository"):
            load_config(repo / ".graylog-mcp.toml", repo_dir=repo)
    (repo / ".graylog-mcp.toml").write_text(f'{body}[ask]\nlanguage = "vi"\n')
    assert load_config(repo / ".graylog-mcp.toml", repo_dir=repo).ask.language == "vi"
    outside = tmp_path / "central.toml"  # a user or central config may choose
    outside.write_text(f'{body}[ask]\nbase_url = "https://proxy.example.com"\n')
    assert load_config(outside, repo_dir=repo).ask.base_url == "https://proxy.example.com"


async def test_the_question_is_redacted_before_it_leaves(make_ask_app):
    jev = FakeJev(relevant=("nothing",))
    app, _ = make_ask_app(jev)
    await ask(app, "why did this fail for alice@example.com with password=hunter2?")
    sent = json.dumps(jev.bodies)
    assert "alice@example.com" not in sent and "hunter2" not in sent


async def test_neutral_question_is_not_sent_back_as_unclear(make_ask_app):
    app, _ = make_ask_app(FakeJev(intent="count", errors_only=False))
    out = await ask(app, "how many messages today?")
    assert "clarify" not in out and out["result"]["count"] > 0


def test_a_project_config_outside_git_cannot_choose_where_the_key_goes(key, tmp_path):
    from graylog_mcp.config import load_config

    unpacked = tmp_path / "archive"  # no .git anywhere: not recognised as a repository
    unpacked.mkdir()
    body = '[instances.main]\nurl = "https://graylog.test"\ntoken_env = "TEST_GRAYLOG_TOKEN"\n'
    (unpacked / ".graylog-mcp.toml").write_text(f'{body}[ask]\nbase_url = "https://evil.test"\n')
    with pytest.raises(ConfigError, match="not allowed in a repository"):
        load_config(unpacked / ".graylog-mcp.toml", repo_dir=unpacked)


def test_login_ask_tests_and_saves_the_key_model_and_endpoint(monkeypatch, capsys):
    from graylog_mcp import secrets
    from graylog_mcp.__main__ import main
    from graylog_mcp.ask import jev as jev_module

    monkeypatch.setenv("TYPESAFE_API_KEY", "")  # nothing saved yet
    jev = FakeJev()
    monkeypatch.setattr(jev_module, "TRANSPORT", jev.transport)
    monkeypatch.setattr("graylog_mcp.__main__._read_secret", lambda prompt: "fake-typesafe-key")

    rc = main(["login", "--ask", "--model", "jev-preview", "--base-url", "https://llm.example.com"])

    assert rc == 0 and "accepted the key" in capsys.readouterr().out
    assert jev.bodies[0]["model"] == "jev-preview"
    assert secrets.get("TYPESAFE_API_KEY") == "fake-typesafe-key"
    cfg = make_config().ask
    assert (cfg.model, cfg.base_url, cfg.enabled) == ("jev-preview", "https://llm.example.com", True)

    assert main(["logout", "--ask", "main"]) == 2
    assert main(["logout", "--ask"]) == 0
    cfg = make_config().ask
    assert (cfg.model, cfg.base_url, cfg.enabled) == ("jev-latest", "https://api.typesafe.ai", False)


def test_login_ask_saves_nothing_when_the_key_is_refused(monkeypatch, capsys):
    from graylog_mcp import secrets
    from graylog_mcp.__main__ import main
    from graylog_mcp.ask import jev as jev_module

    monkeypatch.setenv("TYPESAFE_API_KEY", "")
    monkeypatch.setattr(jev_module, "TRANSPORT", FakeJev(status=401).transport)
    monkeypatch.setattr("graylog_mcp.__main__._read_secret", lambda prompt: "fake-typesafe-key")
    assert main(["login", "--ask"]) == 1
    assert secrets.get("TYPESAFE_API_KEY") is None
    assert main(["login", "--ask", "--base-url", "http://x.test"]) == 2


async def test_a_code_that_looks_like_a_card_number_is_still_looked_up(make_ask_app):
    jev = FakeJev(intent="trace_code")
    app, fake = make_ask_app(jev, dataset="basic", ask={"code_patterns": [r"\d{16}"]})
    res = await ask(app, "what happened to transaction 4111111111111111?")
    searched = " ".join(str(r.url) + r.content.decode() for r in fake.requests)
    assert "4111111111111111" in searched
    assert "4111111111111111" not in json.dumps(jev.bodies) and "4111111111111111" not in json.dumps(res["codes"])


async def test_three_long_traces_stay_under_the_size_cap(make_ask_app):
    jev = FakeJev(intent="trace_code")
    app, fake = make_ask_app(jev, dataset="basic", ask={"code_patterns": [r"ORD-\d+"]})
    fake.messages = []
    for code in ("ORD-1", "ORD-2", "ORD-3"):
        for i in range(200):
            _add(fake, 900 - i, service="gateway", level=6, message=f"{code} step {i} " + "x" * 400)
    res = await ask(app, "show ORD-1, ORD-2 and ORD-3")
    assert len(res["traces"]) == 3 and len(dumps(res)) <= app.config.limits.max_output_chars


async def test_traces_with_unlinked_lines_stay_under_the_size_cap(make_ask_app):
    jev = FakeJev(intent="trace_code")
    app, fake = make_ask_app(jev, dataset="basic", ask={"code_patterns": [r"ORD-\d+"]})
    fake.messages = []
    for n, code in enumerate(("ORD-1", "ORD-2", "ORD-3")):
        _add(fake, 900, trace_id=f"req-{n}", service="gateway", message=f"{code} received")
        for i in range(20):
            _add(fake, 800 - i, service="batch", message=f"{code} export {i} " + "x" * 900)
    res = await ask(app, "show ORD-1, ORD-2 and ORD-3")
    assert len(res["traces"]) == 3 and len(dumps(res)) <= app.config.limits.max_output_chars


async def test_a_masked_code_is_not_echoed_by_its_trace(make_ask_app):
    app, _ = make_ask_app(FakeJev(intent="trace_code"), dataset="basic", ask={"code_patterns": [r"\d{16}"]})
    res = await ask(app, "what happened to transaction 4111111111111111?")
    assert "4111111111111111" not in dumps(res) and res["traces"][0]["trace_id"] == "[CARD]"


PAYMENT = {"aliases": ["thanh toán"], "query": "service:payment", "description": "payment service"}
TERMS = {"terms": {"payment": PAYMENT}}
REQUEST_CODES = {"codes": {"request": {"pattern": r"req-[0-9a-f]{6}"}}}


async def test_a_glossary_term_narrows_the_count_and_is_explained(make_ask_app):
    app, _ = make_ask_app(FakeJev(intent="count"), glossary=TERMS, ask={"language": "vi"})
    everything = (await ask(app, "có bao nhiêu lỗi"))["result"]["count"]
    out = await ask(app, "thanh toán có bao nhiêu lỗi")
    assert 0 < out["result"]["count"] < everything
    assert out["understood"]["topics"] == [{"term": "payment", "matched": "thanh toán", "query": "service:payment"}]
    assert 'Đã hiểu "thanh toán" là payment service, lọc theo service:payment.' in out["summary"]
    assert out["cost"]["jev_requests"] == 1


async def test_a_question_without_a_term_has_no_topics(make_ask_app):
    app, _ = make_ask_app(FakeJev(intent="count"), glossary=TERMS)
    out = await ask(app, "how many errors?")
    assert "topics" not in out["understood"] and "unclear" not in out["understood"]
    understood = "Understood: main, last hour, technical and business errors. Say so if this is not what you meant."
    first, *rest = out["summary"].splitlines()
    assert first == understood and len(rest) == 1 and "defines no business errors" in rest[0]


@pytest.mark.parametrize("intent", ["error_summary", "root_cause", "compare"])
async def test_a_glossary_term_narrows_the_other_tools_too(make_ask_app, intent):
    app, fake = make_ask_app(FakeJev(intent=intent), glossary=TERMS)
    await ask(app, "thanh toán thế nào")
    assert any("service:payment" in _sent(r) for r in fake.requests)


def _sent(request):
    return " ".join(v for _, v in request.url.params.multi_items()) + request.content.decode()


async def test_a_glossary_term_narrows_the_error_groups_of_a_flow(make_ask_app):
    app, fake = make_ask_app(FakeJev(relevant=("bank call failed",)), glossary=TERMS)
    out = await ask(app, "thanh toán bị lỗi gọi ngân hàng")
    assert out["matches"] and out["flow"]["first_error"]["service"] == "payment"
    assert any("service:payment" in _sent(r) for r in fake.requests)
    # the flow itself is not narrowed: it crosses services
    assert {"gateway", "payment"} <= {s["service"] for s in out["flow"]["services"]}


async def test_words_typed_without_accents_that_fit_two_terms_are_asked_about(make_ask_app):
    terms = {"code": {"aliases": ["mã"], "keywords": ["code"]}, "cheek": {"aliases": ["má"], "keywords": ["cheek"]}}
    app, _ = make_ask_app(FakeJev(intent="count"), glossary={"terms": terms})
    out = await ask(app, "ma co bao nhieu loi")
    assert "topics" not in out["understood"]
    assert out["understood"]["unclear"] == [{"text": "ma", "terms": ["code", "cheek"]}]
    assert '"ma" could be code, cheek; write it with its accents or say more.' in out["summary"]


async def test_a_term_with_keywords_only_is_explained_and_filters_nothing(make_ask_app):
    terms = {"refund": {"aliases": ["hoàn tiền"], "keywords": ["refund"]}}
    app, _ = make_ask_app(FakeJev(intent="count"), glossary={"terms": terms})
    everything = (await ask(app, "how many errors?"))["result"]["count"]
    out = await ask(app, "hoàn tiền có bao nhiêu lỗi")
    assert out["result"]["count"] == everything
    assert out["understood"]["topics"] == [{"term": "refund", "matched": "hoàn tiền", "keywords": ["refund"]}]
    assert '"hoàn tiền" is understood as refund; this term does not narrow the search.' in out["summary"]


async def test_two_filtering_terms_say_that_they_narrow_together(make_ask_app):
    terms = {"payment": PAYMENT, "gateway": {"aliases": ["cổng"], "query": "service:gateway"}}
    app, _ = make_ask_app(FakeJev(intent="count"), glossary={"terms": terms})
    out = await ask(app, "thanh toán và cổng có bao nhiêu lỗi")
    assert out["result"]["count"] == 0
    assert "These 2 terms narrow the search together: only lines that match all of them count." in out["summary"]
    one = await ask(app, "thanh toán có bao nhiêu lỗi")
    assert "narrow the search together" not in one["summary"]


async def test_a_refused_glossary_query_names_the_term(make_ask_app):
    terms = {"payment": {"aliases": ["thanh toán"], "query": "service:(payment"}}
    app, fake = make_ask_app(FakeJev(intent="count"), glossary={"terms": terms})
    wanted = r"glossary term 'payment': Graylog cannot read its query \(.*\); fix glossary\.terms\.payment\.query"
    with pytest.raises(ToolInputError, match=wanted):
        await ask(app, "thanh toán có bao nhiêu lỗi")
    asked = [r.url.path.rsplit("/api/", 1)[-1] for r in fake.requests]
    assert asked.count("search/validate") == 1 and not any("universal" in p or "views/search" in p for p in asked)


async def test_a_term_on_a_field_this_range_does_not_hold_is_not_a_broken_query(make_ask_app):
    terms = {"stock": {"aliases": ["kho"], "query": "warehouse_zone:north"}}
    app, _ = make_ask_app(FakeJev(intent="count"), glossary={"terms": terms})
    assert (await ask(app, "kho có bao nhiêu lỗi"))["result"]["count"] == 0


async def test_a_time_phrase_is_read_without_asking_the_provider(make_ask_app):
    jev = FakeJev(intent="count")
    app, _ = make_ask_app(jev, glossary=TERMS)
    out = await ask(app, "thanh toán lỗi trong 15 phút qua")
    assert set(jev.bodies[0]["questions"]) == {"intent"}
    assert out["understood"]["time"]["words"] == "trong 15 phút qua"
    assert out["understood"]["sources"] == {"intent": "jev", "instance": "default", "time": "rules", "errors": "rules"}
    assert out["understood"]["errors"] == "both"
    first, last = (datetime.fromisoformat(out["understood"]["time"][k]) for k in ("from", "to"))
    assert (last - first).total_seconds() == 900


async def test_without_a_time_phrase_or_an_error_word_the_provider_is_asked(make_ask_app):
    jev = FakeJev(intent="count", errors_only=False)
    app, _ = make_ask_app(jev)
    out = await ask(app, "thanh toán bị gì")
    assert {"intent", "time", "errors_only"} <= set(jev.bodies[0]["questions"])
    assert out["understood"]["sources"]["time"] == "jev" and out["understood"]["errors"] == "any"
    assert "all lines" in out["summary"]


async def test_a_count_of_errors_is_split_by_kind(make_ask_app):
    app, fake = make_ask_app(FakeJev(intent="count"), dataset="basic")
    _add(fake, 30, service="gateway", http_status=500, message="upstream gone")
    _add(fake, 20, service="gateway", http_status=422, message="amount refused")
    out = await ask(app, "có bao nhiêu lỗi")
    kinds = out["result"]["by_kind"]
    assert kinds["technical"] > 0 and kinds["business"] > 0
    assert out["result"]["count"] == kinds["technical"] + kinds["business"]
    assert "http_status:[500 TO 599]" in out["ran"] and "http_status:[400 TO 499]" in out["ran"]


async def test_business_errors_on_an_instance_without_a_definition_say_so(make_ask_app):
    app, fake = make_ask_app(FakeJev(intent="count"), dataset="basic")
    for m in fake.messages:
        m.pop("http_status", None)
    out = await ask(app, "giao dịch bị từ chối hôm nay")
    assert out["understood"]["errors"] == "business" and "defines no business errors" in out["summary"]
    assert "by_kind" not in out["result"]


async def test_a_search_question_returns_lines_filtered_by_the_term(make_ask_app):
    terms = {"payment": PAYMENT | {"keywords": ["bank"]}}
    app, fake = make_ask_app(FakeJev(intent="search", errors_only=False), glossary={"terms": terms})
    out = await ask(app, "thanh toán hôm nay")
    assert out["result"]["messages"] and out["ran"] == '(service:payment) AND ("bank")'
    assert any(out["ran"] in _sent(r) for r in fake.requests)


async def test_an_error_without_a_trace_id_shows_the_lines_around_it(make_ask_app):
    app, _ = make_ask_app(FakeJev(relevant=("deadlock",), time="last_7_days"))
    out = await ask(app, "database deadlock")
    assert "flow" not in out and out["around"]["messages"]
    assert "no trace id" in out["hint"] and "not one request" in out["hint"]


async def test_the_acceptance_question_needs_one_provider_call(make_ask_app):
    def instance(name):
        return {"url": f"https://{name}.test", "token_env": "TEST_GRAYLOG_TOKEN"}

    jev = FakeJev(intent="count")
    app, _ = make_ask_app(jev, instances={"prod": instance("prod"), "dev": instance("dev")}, glossary=TERMS)
    out = await ask(app, "thanh toán lỗi trong 15 phút qua ở prod", instance="main")
    assert len(jev.bodies) == 1 and set(jev.bodies[0]["questions"]) == {"intent"}
    assert out["understood"]["sources"]["instance"] == "caller" and out["understood"]["errors"] == "both"
    assert out["understood"]["time"]["words"] == "trong 15 phút qua"
    assert '"thanh toán" is understood as payment service, filtered by service:payment.' in out["summary"]
    assert out["ran"].endswith("AND (service:payment)")


async def test_codes_come_from_the_glossary_and_from_the_old_setting(make_ask_app):
    jev = FakeJev(intent="trace_code")
    app, _ = make_ask_app(jev, dataset="basic", glossary=REQUEST_CODES, ask={"code_patterns": [r"ORD-\d+"]})
    out = await ask(app, "xem ORD-1 và req-7f3a9c giúp")
    assert out["codes"] == ["ORD-1", "req-7f3a9c"]  # in the order of the question


async def test_a_code_question_is_not_narrowed_by_a_term(make_ask_app):
    app, _ = make_ask_app(FakeJev(intent="trace_code"), dataset="basic", glossary=TERMS | REQUEST_CODES)
    out = await ask(app, "thanh toán req-7f3a9c đi qua đâu")
    assert "topics" not in out["understood"] and len(out["traces"][0]["timeline"]) == 6


async def test_nothing_about_the_glossary_is_sent_to_typesafe(make_ask_app):
    jev = FakeJev(intent="count")
    app, _ = make_ask_app(jev, glossary=TERMS)
    await ask(app, "thanh toán có bao nhiêu lỗi")
    assert len(jev.bodies) == 1 and "service:payment" not in json.dumps(jev.bodies)
