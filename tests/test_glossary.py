"""The glossary: business terms and code shapes read from the config, and matched in a question."""

from __future__ import annotations

import re
import tomllib
import unicodedata
from pathlib import Path

import pytest

from graylog_mcp.config import ConfigError
from graylog_mcp.glossary import code_kind, error_kind, find_codes, fold, match_terms, match_words, strip_marks
from graylog_mcp.setup import configfile
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
        ({"codes": {"c": {"fields": ["order_id"]}}}, "glossary.codes.c.pattern: expected a regular expression"),
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


@pytest.mark.parametrize(
    ("question", "kind"),
    [
        ("thanh toán bị lỗi", ("both", "lỗi")),
        ("thanh toan bi loi", ("both", "loi")),
        ("giao dịch bị từ chối", ("business", "bị từ chối")),
        ("timeout khi gọi ngân hàng", ("technical", "timeout")),
        ("lỗi hệ thống sáng nay", ("technical", "lỗi hệ thống")),
        ("declined or timeout", ("both", "declined timeout")),
        ("Payment FAILED twice", ("both", "FAILED")),
        ("có bao nhiêu dòng", None),
        ("lời chào", None),
        ("errorless and failsafe", None),
    ],
)
def test_the_kind_of_error_is_read_from_the_words(question, kind):
    assert error_kind(make_config().glossary.error_words, question) == kind


def test_error_words_come_from_the_config():
    cfg = make_config(glossary={"error_words": {"business": ["hết hạn mức"]}})
    words = cfg.glossary.error_words
    assert words.business == ("hết hạn mức",) and "timeout" in words.technical and "lỗi" in words.either
    assert error_kind(words, "khách bị hết hạn mức") == ("business", "hết hạn mức")
    assert error_kind(words, "giao dịch bị từ chối") is None
    for wrong in ({"fatal": ["x"]}, {"business": "declined"}, {"technical": [""]}):
        with pytest.raises(ConfigError, match=r"glossary\.error_words"):
            make_config(glossary={"error_words": wrong})


def test_words_are_matched_in_the_order_of_the_text():
    groups = {"fruit": ["táo", "cam sành"], "colour": ["cam"]}
    assert match_words(groups, "Cam sành, tao và cam") == [
        ("fruit", "Cam sành", 0, 8),
        ("fruit", "tao", 10, 13),
        ("colour", "cam", 17, 20),
    ]


def test_the_example_config_holds_a_glossary_that_loads():
    example = Path(__file__).resolve().parents[1] / "examples" / "config.toml"
    data = tomllib.loads(example.read_text(encoding="utf-8"))
    cfg = make_config(glossary=data["glossary"], presets=data["presets"])
    assert cfg.glossary.terms and cfg.glossary.codes


def test_saving_settings_from_the_admin_page_keeps_the_glossary():
    main = {"url": "https://graylog.test", "token_env": "TEST_GRAYLOG_TOKEN"}
    data = {"instances": {"main": main}, "glossary": GLOSSARY}
    saved = configfile.render(configfile.write_scope(data, "global", {"error_query": "level:<=4"}))
    kept = configfile.validate_text(saved)
    assert kept.instances["main"].error_query == "level:<=4"
    assert kept.glossary == make_config(glossary=GLOSSARY).glossary


def _matched(question, glossary=GLOSSARY):
    cfg = make_config(glossary=glossary)
    found, unclear = match_terms(cfg.glossary, cfg.instances["main"], question)
    return [(m.term.name, m.text, m.exact) for m in found], [(u.text, u.terms) for u in unclear]


def test_the_longest_phrase_wins():
    assert _matched("Cổng Thanh  Toán bị lỗi") == ([("gateway", "Cổng Thanh  Toán", True)], [])
    assert _matched("thanh toán bị lỗi, payment timeout") == ([("payment", "thanh toán", True)], [])


def test_a_question_typed_without_accents_or_decomposed_still_matches():
    assert _matched("cong thanh toan loi") == ([("gateway", "cong thanh toan", False)], [])
    assert _matched(unicodedata.normalize("NFD", "hoàn tiền chậm")) == ([("refund", "hoàn tiền", True)], [])


def test_an_alias_inside_a_longer_word_does_not_match():
    assert _matched("refunded twice, thanh toánh, payment2") == ([], [])
    assert _matched("order-refund, ok") == ([("refund", "refund", True)], [])


def test_several_terms_come_in_the_order_of_the_question():
    found, _ = _matched("hoàn tiền sau khi thanh toán")
    assert [name for name, _, _ in found] == ["refund", "payment"]


def test_an_accented_word_never_matches_another_accented_alias():
    terms = {"code": {"aliases": ["mã"], "keywords": ["code"]}, "cheek": {"aliases": ["má"], "keywords": ["cheek"]}}
    assert _matched("mã bị sai", {"terms": terms}) == ([("code", "mã", True)], [])
    assert _matched("ma bi sai", {"terms": terms}) == ([], [("ma", ("code", "cheek"))])


@pytest.mark.parametrize("aliases", [["mã", "ma"], ["ma", "mã"]])
def test_an_alias_written_without_accents_settles_what_the_bare_word_means(aliases):
    terms = {"code": {"aliases": aliases, "keywords": ["code"]}, "cheek": {"aliases": ["má"], "keywords": ["cheek"]}}
    assert _matched("ma bi sai", {"terms": terms}) == ([("code", "ma", True)], [])


@pytest.mark.parametrize(
    ("alias", "typed"),
    [("hóa đơn", "hoá đơn"), ("hoá đơn", "hóa đơn"), ("hủy đơn", "Huỷ đơn"), ("khoá", "khóa"), ("thúy", "THUÝ")],
)
def test_both_places_of_a_tone_mark_are_one_word(alias, typed):
    terms = {"t": {"aliases": [alias], "keywords": ["x"]}}
    assert _matched(f"{typed} bị lỗi", {"terms": terms}) == ([("t", typed, True)], [])


def test_another_tone_is_another_word():
    terms = {"t": {"aliases": ["hóa đơn"], "keywords": ["x"]}}
    assert _matched("hòa đơn và hoà đơn", {"terms": terms}) == ([], [])


def test_two_spellings_of_one_word_are_one_alias():
    terms = {"a": {"aliases": ["hoá đơn"], "keywords": ["x"]}, "b": {"aliases": ["hóa đơn"], "keywords": ["y"]}}
    with pytest.raises(ConfigError, match="belongs to both terms 'a' and 'b'"):
        make_config(glossary={"terms": terms})


def test_an_unclear_word_is_reported_once():
    terms = {"code": {"aliases": ["mã"], "keywords": ["code"]}, "cheek": {"aliases": ["má"], "keywords": ["cheek"]}}
    assert _matched("ma hay ma", {"terms": terms}) == ([], [("ma", ("code", "cheek"))])


def test_an_unaccented_alias_wins_over_a_stripped_one():
    terms = {
        "refund": {"aliases": ["hoàn tiền"], "keywords": ["refund"]},
        "club": {"aliases": ["hoan tien"], "keywords": ["club"]},
    }
    assert _matched("hoan tien", {"terms": terms}) == ([("club", "hoan tien", True)], [])
    assert _matched("hoàn tiền", {"terms": terms}) == ([("refund", "hoàn tiền", True)], [])


def test_terms_of_another_instance_do_not_match():
    terms = {"payment": {"aliases": ["thanh toán"], "query": "service:payment", "instances": ["elsewhere"]}}
    assert _matched("thanh toán lỗi", {"terms": terms}) == ([], [])


def test_codes_are_found_in_the_order_of_the_question():
    codes = {"order": {"pattern": r"ORD-\d+", "fields": ["order_id"]}, "any": {"pattern": r"[A-Z]+-\d+"}}
    cfg = make_config(glossary={"codes": codes}, ask={"code_patterns": [r"\b\d{10}\b"]})
    found = find_codes(cfg.glossary, cfg.instances["main"], "TXN-7 rồi ORD-1001, lại ORD-1001 và 4111111111")
    assert [(c.kind.name, c.value) for c in found] == [
        ("any", "TXN-7"),
        ("order", "ORD-1001"),
        ("ask_code_1", "4111111111"),
    ]


def test_code_kind_needs_the_whole_value():
    cfg = make_config(glossary=GLOSSARY)
    main = cfg.instances["main"]
    assert code_kind(cfg.glossary, main, "ORD-1001").name == "order"
    assert code_kind(cfg.glossary, main, "ORD-1001x") is None and code_kind(cfg.glossary, main, "req-7f3a9c") is None
