"""The glossary: business terms and code shapes read from the config, and matched in a question."""

from __future__ import annotations

import re
import unicodedata

import pytest

from graylog_mcp.config import ConfigError
from graylog_mcp.glossary import fold, strip_marks
from tests.conftest import make_config

GLOSSARY = {
    "terms": {
        "payment": {"aliases": ["thanh toán", "payment"], "query": "service:payment", "description": "payment service"},
        "gateway": {"aliases": ["cổng thanh toán"], "query": "service:gateway"},
        "refund": {"aliases": ["hoàn tiền", "refund"], "keywords": ["refund"]},
    },
    "codes": {"order": {"pattern": r"ORD-\d+", "fields": ["order_id"]}},
}


def test_a_glossary_is_read_from_the_config():
    cfg = make_config(glossary=GLOSSARY, ask={"code_patterns": [r"TXN\d+"]})
    main = cfg.instances["main"]
    assert [t.name for t in cfg.glossary.terms_for(main)] == ["payment", "gateway", "refund"]
    assert cfg.glossary.terms["refund"].keywords == ("refund",) and cfg.glossary.terms["refund"].query == ""
    assert [(c.name, c.pattern, c.fields) for c in cfg.glossary.codes_for(main)] == [
        ("order", r"ORD-\d+", ("order_id",)),
        ("ask_code_1", r"TXN\d+", ()),
    ]


def test_no_glossary_is_an_empty_one():
    cfg = make_config()
    assert cfg.glossary.terms == {} and cfg.glossary.codes == {}


@pytest.mark.parametrize(
    ("glossary", "message"),
    [
        ({"terms": {"a": {"aliases": [], "query": "x:1"}}}, "glossary.terms.a.aliases: expected at least one alias"),
        ({"terms": {"a": {"aliases": ["--"], "query": "x:1"}}}, "'--' has no letter or digit"),
        ({"terms": {"a": {"aliases": ["a"]}}}, "glossary.terms.a: set query, keywords or preset"),
        ({"terms": {"a": {"aliases": ["a"], "preset": "nope"}}}, "unknown preset 'nope'; available: none configured"),
        ({"terms": {"a": {"aliases": ["a"], "query": " "}}}, "glossary.terms.a.query"),
        ({"terms": {"a": {"aliases": ["a"], "query": "x:1", "colour": 1}}}, "colour"),
        ({"codes": {"c": {"pattern": "("}}}, "glossary.codes.c.pattern: '(' is not a regular expression"),
        ({"codes": {"c": {"pattern": r"\d*"}}}, "matches empty text"),
        ({"words": {}}, "words"),
    ],
)
def test_a_wrong_glossary_stops_the_load_with_the_reason(glossary, message):
    with pytest.raises(ConfigError, match=re.escape(message)):
        make_config(glossary=glossary)


def test_one_alias_for_two_terms_is_refused():
    terms = {
        "payment": {"aliases": ["Thanh  Toán"], "query": "service:payment"},
        "billing": {"aliases": ["thanh toán"], "query": "service:billing"},
    }
    wanted = "alias 'thanh toán' belongs to both terms 'payment' and 'billing' on instance 'main'"
    with pytest.raises(ConfigError, match=wanted):
        make_config(glossary={"terms": terms})


def test_aliases_that_differ_only_by_accents_are_two_aliases():
    terms = {"code": {"aliases": ["mã"], "keywords": ["code"]}, "cheek": {"aliases": ["má"], "keywords": ["cheek"]}}
    assert len(make_config(glossary={"terms": terms}).glossary.terms) == 2


def test_two_groups_may_give_one_word_to_different_terms():
    def instance(group):
        url = f"https://{group}.test"
        return {"url": url, "token_env": "TEST_GRAYLOG_TOKEN", "group": group, "environment": "prod"}

    instances = {"shop-prod": instance("shop"), "bank-prod": instance("bank")}
    terms = {
        "shop_pay": {"aliases": ["thanh toán"], "query": "service:checkout", "instances": ["shop"]},
        "bank_pay": {"aliases": ["thanh toán"], "query": "service:transfer", "instances": ["bank"]},
    }
    cfg = make_config(instances=instances, glossary={"terms": terms})
    assert [t.name for t in cfg.glossary.terms_for(cfg.instances["bank-prod"])] == ["bank_pay"]
    assert cfg.glossary.terms_for(cfg.instances["main"]) == []
    terms["bank_pay"]["instances"] = ["prod"]
    with pytest.raises(ConfigError, match="on instance 'shop-prod'"):
        make_config(instances=instances, glossary={"terms": terms})


def test_a_term_may_point_at_a_preset():
    presets = {"refund_errors": {"tool": "search_logs", "args": {"query": "refund AND level:<=3"}}}
    terms = {"refund": {"aliases": ["hoàn tiền"], "preset": "refund_errors"}}
    assert make_config(presets=presets, glossary={"terms": terms}).glossary.terms["refund"].preset == "refund_errors"


def test_fold_and_strip_marks_keep_every_position():
    typed = unicodedata.normalize("NFD", "Cổng Thanh Toán Đỏ")
    assert fold(typed) == "cổng thanh toán đỏ" and strip_marks(fold(typed)) == "cong thanh toan do"
    assert len(fold("İstanbul")) == len(unicodedata.normalize("NFC", "İstanbul"))
