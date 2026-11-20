"""How much of the sample questions the rules read right: the measure taken before and after a change to them.

Run with ``-s`` to see the table.
"""

from __future__ import annotations

from datetime import datetime

from graylog_mcp.ask.intent import read_instance, read_intent
from graylog_mcp.ask.when import read_time
from graylog_mcp.glossary import error_kind, find_codes, match_terms
from tests.conftest import make_config
from tests.data.held_questions import HELD
from tests.data.questions import GLOSSARY, NOW, QUESTIONS, ZONE

THRESHOLDS = {"time": 0.9, "terms": 0.9, "codes": 0.9, "errors": 0.8}


def _at(text: str) -> datetime:
    day, _, clock = text.rpartition(" ")
    return datetime.fromisoformat(f"2026-{day or '11-19'} {clock}").replace(tzinfo=ZONE)


def _read(question: str) -> dict[str, object]:
    cfg = make_config(glossary=GLOSSARY)
    main = cfg.instances["main"]
    span = read_time(question, ZONE, NOW, cfg.ask.time)
    kind = error_kind(cfg.glossary.error_words, question)
    return {
        "time": (span.start, span.end) if span else None,
        "terms": [m.term.name for m in match_terms(cfg.glossary, main, question)[0]],
        "codes": [c.value for c in find_codes(cfg.glossary, main, question)],
        "errors": kind[0] if kind else None,
    }


def test_the_rules_read_the_sample_questions():
    wrong: dict[str, list[str]] = {name: [] for name in THRESHOLDS}
    for question, time, terms, codes, errors in QUESTIONS:
        span = (_at(time[0]), _at(time[1])) if time else None
        expected = {"time": span, "terms": terms, "codes": codes, "errors": errors}
        got = _read(question)
        for name in THRESHOLDS:
            if got[name] != expected[name]:
                wrong[name].append(f"{question!r}: read {got[name]!r}, meant {expected[name]!r}")
    shares = {name: 1 - len(missed) / len(QUESTIONS) for name, missed in wrong.items()}
    print("\n" + "\n".join(f"{name:7} {share:5.0%}  ({len(wrong[name])} wrong)" for name, share in shares.items()))
    for name, missed in wrong.items():
        for line in missed:
            print(f"  {name}: {line}")
    assert len(QUESTIONS) >= 30
    assert all(shares[name] >= floor for name, floor in THRESHOLDS.items()), shares


def test_the_rules_read_what_questions_written_without_them_ask_for():
    server = {"url": "https://graylog.test", "token_env": "TEST_GRAYLOG_TOKEN"}
    cfg = make_config(glossary=GLOSSARY, instances={name: dict(server) for name in ("prod", "dev", "staging")})
    main = cfg.instances["main"]
    right = sure = sure_right = places = 0
    for question, intent, also, environment in HELD:
        has_code = bool(find_codes(cfg.glossary, main, question))
        has_term = bool(match_terms(cfg.glossary, main, question)[0])
        read = read_intent(cfg.ask.intent_words, cfg.glossary.error_words, question, has_code, has_term)
        ok = read.value is not None and read.value in (intent, also)
        right += ok
        sure += read.sure
        sure_right += read.sure and ok
        where = read_instance(cfg, question)
        places += (where.name if where else None) == environment
        if not ok:
            print(f"  meant {intent}, read {read.value} ({'sure' if read.sure else 'guess'}): {question!r}")
    n = len(HELD)
    print(f"\nintent {right / n:.0%}; sure {sure}/{n}, right when sure {sure_right / sure:.0%}; where {places / n:.0%}")
    assert n >= 90
    assert right / n >= 0.7
    assert sure_right / sure >= 0.9  # a reading called sure is not checked with a provider: it must hold
    assert places / n >= 0.9
