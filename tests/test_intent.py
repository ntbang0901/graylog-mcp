from __future__ import annotations

import pytest

from graylog_mcp.ask.intent import read_instance, read_intent
from graylog_mcp.config import ConfigError, parse_config
from tests.conftest import make_config


def _intent(question: str, has_code: bool = False, has_term: bool = False, **ask):
    cfg = make_config(ask=ask) if ask else make_config()
    return read_intent(cfg.ask.intent_words, cfg.glossary.error_words, question, has_code, has_term)


@pytest.mark.parametrize(
    ("question", "intent"),
    [
        ("có bao nhiêu lỗi hôm nay", "count"),
        ("co bao nhieu loi hom nay", "count"),
        ("how many errors today", "count"),
        ("so với hôm qua thì sao", "compare"),
        ("what changed since yesterday", "compare"),
        ("nguyên nhân sự cố là gì", "root_cause"),
        ("vì sao hệ thống lỗi", "root_cause"),
        ("which service broke first", "root_cause"),
        ("các lỗi chính tuần này", "error_summary"),
        ("top errors on prod", "error_summary"),
        ("show me the logs", "search"),
        ("xem log đăng nhập", "search"),
        ("where does it go wrong", "follow_flow"),
        ("đơn này đi qua đâu", "trace_code"),
    ],
)
def test_words_that_name_an_intent_are_read_for_sure(question, intent):
    read = _intent(question)
    assert (read.value, read.sure) == (intent, True)
    assert read.text


def test_a_code_in_the_question_asks_for_its_history_whatever_else_is_said():
    read = _intent("có bao nhiêu lỗi ở đơn này", has_code=True)
    assert (read.value, read.sure) == ("trace_code", True)


def test_two_named_intents_keep_the_other_as_a_reading():
    read = _intent("có bao nhiêu lỗi so với hôm qua")
    assert (read.value, read.sure, read.others) == ("compare", True, ("count",))


def test_an_error_word_alone_is_a_guess_with_its_other_readings():
    read = _intent("thanh toán bị timeout", has_term=True)
    assert (read.value, read.sure) == ("follow_flow", False)
    assert read.others == ("error_summary", "root_cause")


def test_why_without_the_whole_system_is_a_guess_at_one_error():
    read = _intent("why is checkout slow")
    assert (read.value, read.sure) == ("follow_flow", False)


def test_a_term_of_the_team_alone_is_a_guess_at_its_lines():
    read = _intent("thanh toán", has_term=True)
    assert (read.value, read.sure, read.others) == ("search", False, ("error_summary", "follow_flow"))


def test_nothing_read_offers_three_readings():
    read = _intent("xin chào")
    assert (read.value, read.sure, read.text) == (None, False, None)
    assert read.others == ("follow_flow", "error_summary", "search")


def test_words_of_an_intent_come_from_the_config():
    assert _intent("how many today", intent_words={"count": ["tally"]}).value is None
    assert _intent("tally of today", intent_words={"count": ["tally"]}).value == "count"
    # the other intents keep their built-in words
    assert _intent("what changed", intent_words={"count": ["tally"]}).value == "compare"


def test_an_unknown_intent_in_the_config_is_refused():
    with pytest.raises(ConfigError, match=r"ask\.intent_words"):
        make_config(ask={"intent_words": {"delete": ["x"]}})
    with pytest.raises(ConfigError, match=r"ask\.intent_words\.count"):
        make_config(ask={"intent_words": {"count": 5}})


def _shop(**extra):
    env = {"url": "https://graylog.test", "token_env": "TEST_GRAYLOG_TOKEN"}
    data = {
        "groups": {"shop": {"environments": {"prod": dict(env), "dev": dict(env)}}},
        "default_instance": "shop/dev",
        **extra,
    }
    return parse_config(data, source="test")


@pytest.mark.parametrize(
    ("question", "name"),
    [
        ("lỗi ở prod sáng nay", "shop/prod"),
        ("errors on production", "shop/prod"),
        ("trên dev có gì", "shop/dev"),
        ("loi tren moi truong development", "shop/dev"),
        ("shop prod bị gì", "shop/prod"),
        ("SHOP/PROD bị gì", "shop/prod"),
    ],
)
def test_an_environment_named_in_the_question_is_read(question, name):
    read = read_instance(_shop(), question)
    assert read is not None and read.name == name and read.options == ()
    assert read.text


def test_a_question_naming_no_environment_reads_none():
    assert read_instance(_shop(), "thanh toán lỗi sáng nay") is None
    assert read_instance(_shop(), "the developer said prodigy") is None  # whole words only


def test_a_group_alone_is_its_default_environment():
    cfg = parse_config(
        {
            "groups": {
                "shop": {
                    "default_environment": "prod",
                    "environments": {
                        name: {"url": "https://graylog.test", "token_env": "TEST_GRAYLOG_TOKEN"}
                        for name in ("prod", "dev")
                    },
                }
            }
        },
        source="test",
    )
    read = read_instance(cfg, "shop có lỗi gì")
    assert read is not None and read.name == "shop/prod"


def test_an_alias_of_an_instance_is_read():
    env = {"url": "https://graylog.test", "token_env": "TEST_GRAYLOG_TOKEN"}
    cfg = parse_config(
        {"groups": {"shop": {"environments": {"prod": {**env, "aliases": ["môi trường thật", "live"]}, "dev": env}}}},
        source="test",
    )
    for question in ("lỗi ở môi trường thật", "loi o moi truong that", "errors on live"):
        read = read_instance(cfg, question)
        assert read is not None and read.name == "shop/prod", question


def _two_groups(**extra):
    env = {"url": "https://graylog.test", "token_env": "TEST_GRAYLOG_TOKEN"}
    groups = {name: {"environments": {"prod": dict(env), "dev": dict(env)}} for name in ("shop", "bank")}
    return parse_config({"groups": groups, "default_instance": "shop/dev", **extra}, source="test")


def test_an_environment_of_several_groups_is_not_decided():
    read = read_instance(_two_groups(), "lỗi ở prod")
    assert read is not None and read.name is None
    assert read.options == ("bank/prod", "shop/prod")
    assert read.text == "prod"


def test_the_group_named_with_it_decides():
    read = read_instance(_two_groups(), "bank prod có lỗi gì")
    assert read is not None and read.name == "bank/prod"


def test_the_default_group_decides_between_groups():
    read = read_instance(_two_groups(default_group="bank"), "lỗi ở prod")
    assert read is not None and read.name == "bank/prod"


def test_two_environments_named_are_not_decided():
    read = read_instance(_shop(), "so sánh prod và dev")
    assert read is not None and read.name is None
    assert read.options == ("shop/dev", "shop/prod")


def test_a_plain_instance_name_is_read():
    cfg = make_config(
        instances={
            "main": {"url": "https://graylog.test", "token_env": "TEST_GRAYLOG_TOKEN"},
            "staging": {"url": "https://graylog.test", "token_env": "TEST_GRAYLOG_TOKEN"},
        }
    )
    assert read_instance(cfg, "errors on stg today").name == "staging"
    assert read_instance(cfg, "errors today") is None


def _aliased() -> object:
    """Two groups; "môi trường thật" set once for every group's prod, "cửa hàng" for the group shop."""
    env = {"url": "https://graylog.test", "token_env": "TEST_GRAYLOG_TOKEN"}
    groups = {
        "shop": {"aliases": ["cửa hàng"], "environments": {"dev": dict(env), "prod": dict(env)}},
        "bank": {"environments": {"dev": dict(env), "prod": dict(env)}},
    }
    data = {"environments": {"prod": {"aliases": ["môi trường thật"]}}, "groups": groups}
    return parse_config({**data, "default_instance": "bank/dev"}, source="test")


@pytest.mark.parametrize("question", ["lỗi ở môi trường thật", "loi o moi truong that"])
def test_an_alias_several_instances_share_is_not_decided_for_the_asker(question):
    read = read_instance(_aliased(), question)
    assert read is not None and read.name is None
    assert read.options == ("bank/prod", "shop/prod")


def test_an_alias_of_a_group_reads_like_the_group():
    cfg = _aliased()
    assert read_instance(cfg, "lỗi cửa hàng prod").name == "shop/prod"
    assert read_instance(cfg, "lỗi cửa hàng ở môi trường thật").name == "shop/prod"
    alone, named = read_instance(cfg, "lỗi cửa hàng hôm nay"), read_instance(cfg, "lỗi shop hôm nay")
    # the group has no default environment: its alias is no more decided than its name
    assert alone.name is None and alone.options == named.options == ("shop/dev", "shop/prod")


def test_aliases_must_be_a_list_of_words():
    with pytest.raises(ConfigError, match="aliases"):
        make_config(instances={"main": {"url": "https://graylog.test", "token_env": "T", "aliases": 5}})
