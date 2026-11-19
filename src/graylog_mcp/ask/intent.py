"""What a question asks for and where, read by rule from its words.

Words that name an intent ("how many", "so với", "nguyên nhân") are read for sure. An error word or a term alone
only suggests one: that reading is a guess, said as one, with the other readings it could have been.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from graylog_mcp.config import ENVIRONMENT_WORDS, Config, ConfigError, ErrorWords, InstanceConfig
from graylog_mcp.glossary import error_kind, match_words

# where two intents are named the first of these is meant: "how many more than yesterday" compares
ORDER = ("compare", "count", "root_cause", "error_summary", "follow_flow", "search")
UNREAD = ("follow_flow", "error_summary", "search")  # offered when nothing was read


@dataclass(frozen=True)
class IntentRead:
    value: str | None  # a key of understand.INTENTS; None: not read
    sure: bool  # read from words that name it; False: a guess
    text: str | None  # the words it was read from
    others: tuple[str, ...]  # the readings it could also be


def read_intent(
    words: Mapping[str, tuple[str, ...]], errors: ErrorWords, text: str, has_code: bool, has_term: bool
) -> IntentRead:
    """``has_code``: the question holds a code of the glossary. ``has_term``: it names a term of the team."""
    found = match_words(words, text)
    named = {name for name, *_ in found}
    said = {name: " ".join(written for group, written, *_ in found if group == name) for name in named}
    if has_code or "trace_code" in named:
        return IntentRead("trace_code", True, said.get("trace_code"), ())
    if {"why", "whole_system"} <= named:
        named.add("root_cause")
        said.setdefault("root_cause", f"{said['why']} {said['whole_system']}")
    stated = [name for name in ORDER if name in named]
    if stated:
        return IntentRead(stated[0], True, said[stated[0]], tuple(stated[1:]))
    kind = error_kind(errors, text)
    if "why" in named or kind:
        return IntentRead("follow_flow", False, said.get("why") or (kind[1] if kind else None), _others("follow_flow"))
    if has_term:
        return IntentRead("search", False, None, _others("search"))
    return IntentRead(None, False, None, UNREAD)


def _others(guess: str) -> tuple[str, ...]:
    return ("error_summary", "root_cause") if guess == "follow_flow" else ("error_summary", "follow_flow")


@dataclass(frozen=True)
class InstanceRead:
    name: str | None  # None: the words fit several instances, listed in ``options``
    text: str  # the words it was read from
    options: tuple[str, ...] = ()


def read_instance(config: Config, text: str) -> InstanceRead | None:
    """The instance ``text`` names: by an alias, its name, its environment or its group. None when it names none."""
    instances = list(config.instances.values())
    words: dict[str, list[str]] = {}
    for inst in instances:
        words[f"i:{inst.name}"] = [inst.name, *inst.aliases]
    for env in {i.environment for i in instances if i.environment}:
        words[f"e:{env}"] = _with_other_words(env)
    for inst in instances:  # an instance without a group is known by its name alone: "staging" is also "stg"
        if not inst.group:
            words[f"i:{inst.name}"] += _with_other_words(inst.name)[1:]
    for group in {i.group for i in instances if i.group}:
        words[f"g:{group}"] = [group]
    found = match_words(words, text)
    if not found:
        return None
    said = " ".join(dict.fromkeys(written for _, written, *_ in found))
    keys = {key for key, *_ in found}
    groups = {key[2:] for key in keys if key.startswith("g:")}
    named = [i for i in instances if f"i:{i.name}" in keys or (i.environment and f"e:{i.environment}" in keys)]
    if not named:  # a group alone: its default environment
        return _one(config, [i for i in instances if i.group in groups], said, alone=" ".join(sorted(groups)))
    pool = [i for i in named if i.group in groups] or named
    if len(pool) > 1 and config.default_group:
        pool = [i for i in pool if i.group == config.default_group] or pool
    return _one(config, pool, said)


def _with_other_words(name: str) -> list[str]:
    known = next((words for words in ENVIRONMENT_WORDS if name.lower() in words), ())
    return [name, *(w for w in known if w != name.lower())]


def _one(config: Config, pool: list[InstanceConfig], said: str, alone: str | None = None) -> InstanceRead:
    if len(pool) == 1:
        return InstanceRead(pool[0].name, said)
    if alone:
        try:
            return InstanceRead(config.instance(alone).name, said)
        except ConfigError:  # several groups, or a group whose environments have no default
            pass
    return InstanceRead(None, said, tuple(sorted(i.name for i in pool)))
